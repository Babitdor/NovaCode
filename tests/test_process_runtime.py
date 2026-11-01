"""Managed processes keep draining and remain terminable under noisy output."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from novacode_cli.process_manager import ProcessInfo, ProcessManager, stream_subprocess_output


def _command(script: Path) -> str:
    args = [sys.executable, "-u", str(script)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


async def _cleanup(process, descendants=()) -> None:
    # Only these tests' own processes may be terminated, including on assertion failure.
    with contextlib.suppress(psutil.Error):
        descendants = [*descendants, *psutil.Process(process.pid).children(recursive=True)]
    for child in reversed(descendants):
        with contextlib.suppress(psutil.Error):
            child.kill()
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), 3)


def _terminated(process: psutil.Process) -> bool:
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.timeout(20)
async def test_managed_output_is_drained_without_a_callback(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text("print('x' * 1048576)\nprint('finished')\n")
    manager = ProcessManager()
    info = await manager.start_process(_command(script), name="noisy")
    try:
        await asyncio.wait_for(info._process.wait(), 3)
        for _ in range(100):
            if manager.get_process(info.pid) is None:
                break
            await asyncio.sleep(0.01)
        assert info.exit_code == 0
        assert "finished" in info.output
        assert len(info.output) < 210_000
        assert manager.get_process(info.pid) is None
    finally:
        await _cleanup(info._process)


async def test_long_line_does_not_drop_a_live_process_from_tracking():
    manager = ProcessManager()
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 70000 + b"\nready\n")
    exited = asyncio.Event()

    async def wait():
        await exited.wait()
        return 0

    process = SimpleNamespace(stdout=stream, returncode=None, wait=wait)
    info = ProcessInfo(pid=123, name="fake", command="fake", _process=process)
    manager.register_process(info)
    received = []
    reader = asyncio.create_task(manager._stream_output(process, info, received.append))
    try:
        for _ in range(100):
            if "ready" in received or reader.done():
                break
            await asyncio.sleep(0)
        assert "ready" in received
        assert manager.get_process(info.pid) is info
        stream.feed_eof()
        await asyncio.sleep(0)
        assert manager.get_process(info.pid) is info, "EOF alone is not process exit"
        process.returncode = 0
        exited.set()
        await reader
        assert manager.get_process(info.pid) is None
    finally:
        stream.feed_eof()
        process.returncode = 0
        exited.set()
        await reader


@pytest.mark.timeout(20)
@pytest.mark.parametrize("force", [False, True])
async def test_stop_managed_process_stops_descendants(tmp_path, force):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "tree.py"
    script.write_text(
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    manager = ProcessManager()
    info = await manager.start_process(
        _command(script), name="tree", output_callback=lambda _: None
    )
    descendants = []
    try:
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        child = psutil.Process(int(pidfile.read_text()))
        descendants = psutil.Process(info.pid).children(recursive=True)
        assert await manager.stop_process(info.pid, timeout=0.2, force=force)
        for _ in range(100):
            if _terminated(child):
                break
            await asyncio.sleep(0.01)
        assert _terminated(child)
        assert manager.get_process(info.pid) is None
    finally:
        await _cleanup(info._process, descendants)


async def test_test_output_capture_is_bounded_and_yields(monkeypatch):
    stream = asyncio.StreamReader()
    stream.feed_data((b"x" * 79 + b"\n") * 100_000)
    stream.feed_eof()

    async def wait():
        return 0

    process = SimpleNamespace(stdout=stream, stdin=None, returncode=0, wait=wait)

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    pulses = 0
    received = 0
    running = True

    async def pulse():
        nonlocal pulses
        while running:
            pulses += 1
            await asyncio.sleep(0)

    def callback(line):
        nonlocal received
        received += 1

    watcher = asyncio.create_task(pulse())
    try:
        code, output = await stream_subprocess_output("fake", ".", callback)
    finally:
        running = False
        await watcher
    assert code == 0
    assert received == 100_000
    assert len(output) < 210_000
    assert "Full output:" in output
    assert pulses > 20, "ready output must yield between bounded batches"
    from novacode_cli.shell.output import shell_log_dir

    logs = list(shell_log_dir().glob("*.log"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8") == ("x" * 79 + "\n") * 100_000


@pytest.mark.timeout(20)
@pytest.mark.parametrize("cancel", [False, True])
async def test_test_runner_timeout_and_cancel_stop_descendants(tmp_path, cancel):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "tree.py"
    script.write_text(
        "import subprocess,sys,time,os,json\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    task = asyncio.create_task(
        stream_subprocess_output(_command(script), str(tmp_path), lambda _: None, timeout=2)
    )
    child = None
    try:
        for _ in range(150):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        child = psutil.Process(int(pidfile.read_text()))
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        for _ in range(100):
            if _terminated(child):
                break
            await asyncio.sleep(0.01)
        assert _terminated(child)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if child is not None:
            with contextlib.suppress(psutil.Error):
                child.kill()


async def test_test_runner_timeout_covers_wait_after_output_closes(monkeypatch):
    stream = asyncio.StreamReader()
    stream.feed_eof()
    exited = asyncio.Event()
    killed = []

    async def wait():
        await exited.wait()
        return -1

    process = SimpleNamespace(stdout=stream, stdin=None, returncode=None, wait=wait)

    async def spawn(*_args, **_kwargs):
        return process

    async def kill(proc, **_kwargs):
        killed.append(proc)
        proc.returncode = -1
        exited.set()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    monkeypatch.setattr("novacode_cli.process_manager.terminate_process_tree", kill)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            stream_subprocess_output("fake", ".", lambda _: None, timeout=0.01), 1
        )
    assert killed == [process]
