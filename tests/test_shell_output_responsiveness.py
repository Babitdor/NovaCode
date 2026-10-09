"""Noisy commands must leave time for input and retain bounded display state."""

import asyncio
import sys
from collections import deque

from rich.text import Text

from novacode_cli.tui.output_body import OutputBody
from novacode_cli.tui.shell_output import shell_output_batches
from novacode_cli.tui.widgets import OutputLog
from tests.test_tui_sessions import isolated_session_config  # noqa: F401


async def test_buffered_pipe_yields_between_batches_and_preserves_lines():
    stream = asyncio.StreamReader()
    stream.feed_data(b"line\n" * 20000)
    stream.feed_eof()
    pulses = 0
    done = False

    async def pulse():
        nonlocal pulses
        while not done:
            pulses += 1
            await asyncio.sleep(0)

    watcher = asyncio.create_task(pulse())
    count = 0
    async for lines in shell_output_batches(stream):
        assert all(line == "line" for line in lines)
        count += len(lines)
    done = True
    await watcher
    assert count == 20000
    assert pulses > 4, "ready pipe reads must not monopolize the event loop"


async def test_long_lines_and_split_utf8_have_no_readline_limit_or_corruption():
    content = "猫🦉" * 25000
    stream = asyncio.StreamReader()
    stream.feed_data(content.encode() + b"\nlast")
    stream.feed_eof()
    lines = []
    async for batch in shell_output_batches(stream):
        lines.extend(batch)
    assert "".join(lines[:-1]) == content
    assert lines[-1] == "last"
    assert all(len(line) <= 32768 for line in lines)


def test_appending_and_evicting_output_only_wraps_new_lines(monkeypatch):
    from novacode_cli.tui import output_body

    wrapped = []
    original = output_body.divide_line

    def wrap(text, *args, **kwargs):
        wrapped.append(text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(output_body, "divide_line", wrap)
    lines = deque(Text(f"{n}: " + "wide output " * 8) for n in range(1000))
    body = OutputBody(wrap=True, highlighter=None)
    body.replace_lines(lines)
    body._wrap_rows(80)
    assert len(wrapped) == 1000
    for _ in range(100):
        lines.popleft()
        lines.append(Text("new line"))
        body.replace_lines(lines)
        body._wrap_rows(80)
    assert len(wrapped) == 1100, "unchanged retained lines must not wrap every frame"
    assert len(body._wrap_cache) == 1000
    assert {id(line) for line in lines} == set(body._wrap_cache)
    body._wrap_rows(40)
    assert len(wrapped) == 2100
    body.on_unmount()
    assert not body._wrap_cache


def test_large_ansi_string_is_bounded_before_decoding(monkeypatch):
    parsed = []
    original = Text.from_ansi

    def parse(text, *args, **kwargs):
        parsed.append(len(text))
        return original(text, *args, **kwargs)

    monkeypatch.setattr(Text, "from_ansi", parse)
    output = OutputLog(max_chars=1000)
    output.write("discarded " * 100000 + "latest")
    assert parsed == [999]
    assert output.lines[-1].text.endswith("latest")


async def test_real_noisy_shell_batches_writes_and_prompt_remains_usable(tmp_path, monkeypatch):
    from textual.widgets import Static

    from novacode_cli.tui.widgets import PromptInput
    from tests.test_tui_subagent_panel import _app

    app = _app()
    script = tmp_path / "noisy.py"
    script.write_text("import sys\nfor n in range(20000): print('row-' + str(n))\n")
    writes = []
    original = OutputLog.write

    def write(widget, content):
        if widget.has_class("bash-inline-log"):
            writes.append(1)
        return original(widget, content)

    monkeypatch.setattr(OutputLog, "write", write)
    async with app.run_test(size=(100, 35)) as pilot:
        await app._run_bash(f'!"{sys.executable}" "{script}"')
        await pilot.press("r", "e", "a", "d", "y")
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        output = app._transcript().query_one(".bash-inline-log", OutputLog)
        assert "row-19999" in output.lines[-1].text
        assert len(writes) < 200, "a command output burst must not write/scroll for every line"
        header = app._transcript().query_one(".bash-inline-head", Static)
        assert "running" not in str(header.content)
        assert app.query_one("#prompt", PromptInput).value == "ready"


async def test_execute_capture_stays_bounded_while_all_live_output_streams(monkeypatch):
    import tracemalloc

    from novacode_cli import events
    from novacode_cli.shell import jobs
    from novacode_cli.shell.middleware import ShellMiddleware

    class Stream:
        count = 0

        async def read(self, _size):
            self.count += 1
            return b"x" * 16384 if self.count <= 2000 else b""

    class Process:
        stdout = Stream()
        stdin = None
        returncode = 0

        async def wait(self):
            return 0

    async def spawn(*_args, **_kwargs):
        return Process()

    streamed = 0

    def emit(_id, text):
        nonlocal streamed
        streamed += len(text)

    middleware = ShellMiddleware(workspace_root=".", max_output_bytes=4000, timeout=60)
    monkeypatch.setattr(middleware, "_spawn", spawn)
    monkeypatch.setattr(events, "emit_tool_output", emit)
    tracemalloc.start()
    try:
        result = await middleware._async_local_shell("build", tool_call_id="test")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert streamed == 2000 * 16384
    assert result.content.startswith("x" * 4000)
    assert "truncated" in result.content
    assert peak < 2_000_000, "discarded execute output must not be retained until completion"
    assert all(control.command != "build" for control in jobs._live)


async def test_cancel_after_stdout_closes_terminates_and_unregisters_process(monkeypatch):
    from textual.widgets import Static

    from novacode_cli.process_manager import ProcessManager
    from tests.test_tui_subagent_panel import _app

    waiting = asyncio.Event()
    exited = asyncio.Event()

    class Process:
        pid = 987654321
        returncode = None
        stdout = asyncio.StreamReader()

        async def wait(self):
            waiting.set()
            await exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            exited.set()

    process = Process()
    process.stdout.feed_eof()

    async def terminate_tree(proc, **kwargs):
        assert proc is process
        proc.terminate()

    from novacode_cli.shell.middleware import ShellMiddleware
    # This test owns a fake process. Real tree cleanup is exercised separately;
    # never run taskkill against a synthetic PID from a unit-test fixture.
    monkeypatch.setattr(ShellMiddleware, "_terminate_tree", terminate_tree)

    async def spawn(*_args, **_kwargs):
        return process

    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
        await app._run_bash("!quiet-command")
        await asyncio.wait_for(waiting.wait(), timeout=5)
        for worker in app.workers:
            if worker.name == "_bg_shell_worker":
                worker.cancel()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert process.returncode == -15
        assert process.pid not in ProcessManager.get_instance()._processes
        head = app._transcript().query_one(".bash-inline-head", Static)
        assert "cancelled" in str(head.content)
