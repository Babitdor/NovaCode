"""Background shell work must never wait on the UI, a kill, or a thread pool.

Every running command and background task drains on ONE shared event-loop
thread (``shell.jobs.get_background_loop``). Anything that blocks that thread
stalls all of them, so these pin down each thing that used to.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from novacode_cli.shell import jobs
from novacode_cli.shell.middleware import ShellMiddleware

PY = [sys.executable, "-c"]


def _mw(timeout: float = 60.0) -> ShellMiddleware:
    return ShellMiddleware(workspace_root=os.getcwd(), timeout=timeout)


def _wait_for_controls(n: int, limit: float = 10.0) -> None:
    end = time.time() + limit
    while time.time() < end:
        if len(jobs._live) >= n:
            return
        time.sleep(0.05)
    raise AssertionError(f"expected {n} running commands, saw {len(jobs._live)}")


# ── D: Esc reaches every running command, not just the newest ─────────────


@pytest.mark.timeout(40)
def test_esc_stops_parallel_commands() -> None:
    mw = _mw()

    async def both():
        return await asyncio.gather(
            mw._async_local_shell("import time; time.sleep(60)", tool_call_id="a", prog=PY),
            mw._async_local_shell("import time; time.sleep(60)", tool_call_id="b", prog=PY),
        )

    def esc() -> None:
        _wait_for_controls(2)
        time.sleep(0.3)
        jobs.request_kill()

    t = threading.Thread(target=esc)
    t.start()
    start = time.time()
    a, b = asyncio.run(both())
    t.join()
    assert time.time() - start < 15, "both commands die on Esc, not at their timeout"
    assert "killed by user" in a.content and "killed by user" in b.content
    assert jobs._live == []


# ── C: Esc kills the command's whole tree, not only the shell ──────────────


@pytest.mark.timeout(40)
def test_esc_leaves_no_orphaned_child() -> None:
    psutil = pytest.importorskip("psutil")
    pidfile = Path(tempfile.mkdtemp()) / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )

    def esc() -> None:
        end = time.time() + 15
        while not pidfile.exists() and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.3)
        jobs.request_kill()

    t = threading.Thread(target=esc)
    t.start()
    msg = asyncio.run(_mw()._async_local_shell(code, tool_call_id="c", prog=PY))
    t.join()
    assert "killed by user" in msg.content
    child = int(pidfile.read_text())
    time.sleep(0.5)
    alive = psutil.pid_exists(child) and psutil.Process(child).status() != psutil.STATUS_ZOMBIE
    if alive:  # don't leave it behind if the assertion is about to fail
        psutil.Process(child).kill()
    assert not alive, "the child of a killed command must die with it"


# ── B: a force-kill never blocks the shared loop ───────────────────────────


@pytest.mark.timeout(30)
def test_force_kill_never_uses_blocking_subprocess_run(monkeypatch) -> None:
    def blocking_run(*a, **k):
        raise AssertionError("subprocess.run would block every background task")

    monkeypatch.setattr(subprocess, "run", blocking_run)
    mw = _mw()

    async def go() -> int | None:
        proc = await mw._spawn(
            "import time; time.sleep(60)", PY,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await mw._terminate_tree(proc, grace=0)
        await asyncio.wait_for(proc.wait(), timeout=10)
        return proc.returncode

    assert asyncio.run(go()) is not None


# ── E: the async tool path holds no thread while a command runs ────────────


@pytest.mark.timeout(60)
def test_async_path_needs_no_thread_per_command() -> None:
    mw = _mw()
    assert mw._shell_tool.coroutine is not None, "the tool has a native async body"

    async def run() -> float:
        # Two threads for the whole loop: a sync tool parked one per command.
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=2)
        )
        start = time.time()
        results = await asyncio.gather(
            *[
                mw._adispatch_local(
                    "import time; time.sleep(1); print('ok')", tool_call_id=f"t{i}", prog=PY
                )
                for i in range(12)
            ]
        )
        assert all("ok" in r.content for r in results), [r.content for r in results]
        return time.time() - start

    elapsed = asyncio.run(run())
    assert elapsed < 5, f"12 one-second commands took {elapsed:.1f}s: they queued for threads"


# ── A: the UI hand-off never waits for the UI ──────────────────────────────

pytest.importorskip("textual")


@pytest.mark.timeout(60)
def test_tool_output_and_task_events_do_not_wait_for_a_busy_ui() -> None:
    from tests.test_tui_app import _SS, _FakeAgent

    async def run() -> None:
        from novacode_cli.tui.app import NovaApp
        from novacode_cli.ui.ui_elements import TokenTracker

        app = NovaApp(
            agent=_FakeAgent(), assistant_id="nova-agent", session_state=_SS(), backend=None,
            token_tracker=TokenTracker(), image_tracker=None, model_name="m",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            timings: list[float] = []
            events: list[str] = []
            app._on_task_event = lambda ev, job: events.append(ev)

            def producer() -> None:  # the shell's background-loop thread, in effect
                start = time.time()
                for i in range(200):
                    app._on_tool_output("call-1", f"chunk {i}\n")
                app._on_task_event_threadsafe("completed", None)
                timings.append(time.time() - start)

            t = threading.Thread(target=producer)
            t.start()
            time.sleep(1.5)  # the UI thread is busy (this test runs on it)
            t.join(timeout=10)
            assert timings and timings[0] < 0.5, f"producer waited {timings} on a busy UI"
            for _ in range(10):
                await pilot.pause(0.05)
            assert events == ["completed"], "the event still reaches the UI"
            assert app._tool_out_pending == {} and not app._tool_out_scheduled, "output flushed"

    asyncio.run(run())
