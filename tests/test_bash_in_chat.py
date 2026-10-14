"""Shell commands stream in the chat and report output to the agent."""

from __future__ import annotations

import asyncio
import shutil
import sys

import pytest

sys.path.insert(0, "tests")


def _app():
    import test_tui_app as T

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
    )


def test_a_bang_command_streams_in_chat_and_reports_to_agent(tmp_path):
    """A slow chat command streams output and publishes its logs to the agent."""
    from novacode_cli.shell.jobs import get_registry
    from novacode_cli.tui.widgets import OutputLog
    from textual.widgets import Static

    # Hold the command until the UI checks finish, independent of render speed.
    release = tmp_path / "release"
    script = tmp_path / "wait_for_ui.py"
    script.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        "print('tty=' + str(sys.stdin.isatty()), flush=True)\n"
        f"release = Path({str(release)!r})\n"
        "deadline = time.monotonic() + 30\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "print('late', flush=True)\n"
    )
    slow = f'"{sys.executable}" "{script}"'

    async def drive() -> tuple[bool, str, bool, str, list[str]]:
        app = _app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app._run_bash("!" + slow)  # returns once the worker is started
            for _ in range(5):
                await pilot.pause()
            card = app.query_one("#transcript").query(".bash-inline").last()
            head = card.query_one(".bash-inline-head", Static)
            still_running = "running" in str(head.render())
            release.touch()
            await app.workers.wait_for_complete()
            for _ in range(5):
                await pilot.pause()
            out = " ".join(strip.text for strip in card.query_one(OutputLog).lines)
            job = next(job for job in get_registry().list_jobs() if job.command == slow)
            return (
                still_running, out, "running" not in str(head.render()),
                job.output, list(app._pending_job_notes),
            )

    still_running, out, ok, registry_output, notes = asyncio.run(drive())
    assert still_running, "_run_bash must return while the command is still going"
    assert "tty=" in out and "late" in out, out
    assert ok
    assert "late" in registry_output
    assert any("late" in note for note in notes)


def test_double_bang_opens_terminal_view():
    async def drive() -> tuple[list, bool]:
        app = _app()
        started: list = []
        app._pty_shell_worker = lambda *a, **k: started.append((a, k))  # type: ignore[method-assign]
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app._run_bash("!!echo interactive")
            for _ in range(3):
                await pilot.pause()
            return started, len(app.query(".bash-inline")) == 0

    started, no_inline_card = asyncio.run(drive())
    assert started[0][0][0] == "echo interactive"
    assert started[0][1] == {}
    assert no_inline_card


def test_bang_has_a_tty_and_reports_output(tmp_path):
    """The child sees a TTY, receives keys, and publishes its rendered output."""
    from novacode_cli.shell.jobs import get_registry

    script = tmp_path / "interactive.py"
    script.write_text(
        "import sys\n"
        "print('tty=' + str(sys.stdin.isatty()), flush=True)\n"
        "print('ready', flush=True)\n"
        "print(input(), flush=True)\n"
    )
    command = f'"{sys.executable}" "{script}"'

    async def drive() -> tuple[str, str, list[str]]:
        from novacode_cli.tui.terminal_view import TerminalCommandScreen

        app = _app()
        async with app.run_test(size=(140, 40)) as pilot:
            await app._run_bash("!!" + command)
            try:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if isinstance(app.screen, TerminalCommandScreen) and (
                        "ready" in app.screen.terminal.snapshot()
                    ):
                        break
                assert isinstance(app.screen, TerminalCommandScreen)
                assert "ready" in app.screen.terminal.snapshot()
                assert app.screen.terminal.has_focus
                await pilot.press("o", "k", "enter")
                await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10)
                job = get_registry().list_jobs()[-1]
                return job.output, job.status, list(app._pending_job_notes)
            finally:
                for job in get_registry().list_jobs():
                    if job.command == command and job.status == "running":
                        get_registry().terminate(job.id)

    output, status, notes = asyncio.run(drive())
    assert "tty=True" in output
    assert "ready" in output and "ok" in output
    assert status == "done"
    assert any("tty=True" in note for note in notes)


def test_double_bang_can_return_to_chat_while_command_runs(tmp_path):
    """The terminal view returns to Nova without stopping its command."""
    from novacode_cli.shell.jobs import get_registry
    from novacode_cli.tui.widgets import PromptInput

    script = tmp_path / "prompt.py"
    script.write_text(
        "import sys\nprint('tty=' + str(sys.stdin.isatty()), flush=True)\n"
        "print('answer=' + input(), flush=True)\n"
    )
    command = f'"{sys.executable}" "{script}"'

    async def drive() -> tuple[str, str]:
        app = _app()
        async with app.run_test(size=(140, 40)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "!!" + command
            prompt.focus()
            await pilot.press("enter")
            for _ in range(8):
                await pilot.pause()
            await pilot.press("shift+tab")
            await pilot.press("n", "o", "v", "a")
            draft = prompt.value
            job = get_registry().list_jobs()[-1]
            get_registry().terminate(job.id)
            await app.workers.wait_for_complete()
            return draft, job.output

    draft, output = asyncio.run(drive())
    assert draft == "nova"
    assert "tty=True" in output


@pytest.mark.parametrize(
    ("key", "expected"),
    [("down", "1b5b42"), ("space", "20"), ("escape", "1b")],
)
def test_bang_sends_raw_keys_to_node(tmp_path, key: str, expected: str):
    """Node's raw-mode prompts receive menu keys through the embedded PTY."""
    from novacode_cli.shell.jobs import get_registry

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = tmp_path / "raw-menu.js"
    script.write_text(
        "console.log('tty=' + process.stdin.isTTY);\n"
        "process.stdin.setRawMode(true);\n"
        "process.stdin.once('data', data => {"
        " console.log('keys=' + data.toString('hex')); process.exit(0); });\n"
    )
    command = f'"{node}" "{script}"'

    async def drive() -> str:
        from novacode_cli.tui.terminal_view import TerminalCommandScreen

        app = _app()
        async with app.run_test(size=(140, 40)) as pilot:
            await app._run_bash("!!" + command)
            try:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if isinstance(app.screen, TerminalCommandScreen) and (
                        "tty=true" in app.screen.terminal.snapshot()
                    ):
                        break
                assert isinstance(app.screen, TerminalCommandScreen)
                assert app.screen.terminal.has_focus
                assert "tty=true" in app.screen.terminal.snapshot()
                await pilot.press(key)
                await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10)
                return get_registry().list_jobs()[-1].output
            finally:
                for job in get_registry().list_jobs():
                    if job.command == command and job.status == "running":
                        get_registry().terminate(job.id)

    output = asyncio.run(drive())
    assert "tty=true" in output
    assert f"keys={expected}" in output


def test_ctrl_c_in_interactive_terminal_does_not_quit_nova(tmp_path):
    """Ctrl+C interrupts the child while Nova's TUI stays alive."""
    from novacode_cli.shell.jobs import get_registry

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = tmp_path / "wait.js"
    script.write_text("console.log('ready'); setInterval(() => {}, 1000);\n")

    async def drive() -> bool:
        from novacode_cli.tui.terminal_view import TerminalCommandScreen

        app = _app()
        async with app.run_test(size=(100, 35)) as pilot:
            await app._run_bash(f'!!"{node}" "{script}"')
            command = f'"{node}" "{script}"'
            try:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if isinstance(app.screen, TerminalCommandScreen) and (
                        "ready" in app.screen.terminal.snapshot()
                    ):
                        break
                assert isinstance(app.screen, TerminalCommandScreen)
                assert "ready" in app.screen.terminal.snapshot()
                await pilot.press("ctrl+c")
                return app.is_running
            finally:
                for job in get_registry().list_jobs():
                    if job.command == command and job.status == "running":
                        get_registry().terminate(job.id)
                await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10)

    assert asyncio.run(drive())


def test_bang_reports_nonzero_exit():
    """The PTY's process status reaches the job registry and agent note."""
    from novacode_cli.shell.jobs import get_registry

    async def drive() -> tuple[str, int | None, list[str]]:
        app = _app()
        async with app.run_test(size=(100, 35)):
            await app._run_bash("!!exit 3")
            await app.workers.wait_for_complete()
            job = get_registry().list_jobs()[-1]
            return job.status, job.exit_code, list(app._pending_job_notes)

    status, code, notes = asyncio.run(drive())
    assert status == "failed"
    assert code == 3
    assert any("exit 3" in note for note in notes)


def test_a_command_with_no_output_gets_a_small_card():
    """`code .` prints nothing; its card used to fill the whole transcript."""
    from novacode_cli.tui.widgets import OutputLog

    silent = f'"{sys.executable}" -c "pass"'
    chatty = f'"{sys.executable}" -c "print(chr(10).join(str(i) for i in range(200)))"'

    async def drive() -> tuple[int, str, int]:
        app = _app()
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            heights = []
            for cmd in (silent, chatty):
                await app._run_bash("!" + cmd)
                await app.workers.wait_for_complete()
                for _ in range(8):
                    await pilot.pause()
                card = app.query_one("#transcript").query(".bash-inline").last()
                heights.append(card.size.height)
            first = app.query_one("#transcript").query(".bash-inline").first()
            text = " ".join(strip.text for strip in first.query_one(OutputLog).lines)
            return heights[0], text, heights[1]

    silent_h, text, chatty_h = asyncio.run(drive())
    assert "(no output)" in text
    assert silent_h <= 5, f"an empty command should be compact, got {silent_h} rows"
    assert silent_h < chatty_h <= 24, f"a long output must be capped, got {chatty_h} rows"


def test_output_is_shown_literally_and_a_failure_is_marked(tmp_path):
    """Brackets in output are text, not Rich markup; a non-zero exit shows on the row."""
    from novacode_cli.tui.widgets import OutputLog
    from textual.widgets import Static

    script = tmp_path / "fail.py"
    script.write_text("print('[bold] x [/nope]'); raise SystemExit(3)", encoding="utf-8")
    failing = f'"{sys.executable}" "{script}"'

    async def drive() -> tuple[str, str]:
        app = _app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app._run_bash("!" + failing)
            await app.workers.wait_for_complete()
            for _ in range(5):
                await pilot.pause()
            block = app.query_one("#transcript").query(".bash-inline").last()
            head = str(block.query_one(".bash-inline-head", Static).render())
            out = " ".join(strip.text for strip in block.query_one(OutputLog).lines)
            return head, out

    head, out = asyncio.run(drive())
    assert head.rstrip().endswith("exit 3"), (head, out)
    assert "[bold] x [/nope]" in out, out
