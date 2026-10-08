"""``!cmd`` runs in the chat; ``!!cmd`` is the escape hatch to the real terminal."""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "tests")


def _app():
    import test_tui_app as T

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
    )


def test_a_bang_command_does_not_block_the_prompt_and_gets_no_stdin(tmp_path):
    """A slow command must leave the UI usable, and must not be able to read keys."""
    from novacode_cli.tui.widgets import OutputLog
    from textual.widgets import Static

    # Hold the command until the UI checks finish, independent of render speed.
    release = tmp_path / "release"
    script = tmp_path / "wait_for_ui.py"
    script.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        "print(repr(sys.stdin.read()), flush=True)\n"
        f"release = Path({str(release)!r})\n"
        "deadline = time.monotonic() + 30\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "print('late', flush=True)\n"
    )
    slow = f'"{sys.executable}" "{script}"'

    async def drive() -> tuple[bool, str, bool]:
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
            out = "\n".join(strip.text for strip in card.query_one(OutputLog).lines)
            return still_running, out, "running" not in str(head.render())

    still_running, out, ok = asyncio.run(drive())
    assert still_running, "_run_bash must return while the command is still going"
    assert "''" in out and "late" in out, out
    assert ok


def test_a_double_bang_takes_the_terminal_path_not_the_card():
    async def drive() -> tuple[list, bool]:
        app = _app()
        started: list = []
        app._bg_shell_worker = lambda *a, **k: started.append((a, k))  # type: ignore[method-assign]
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app._run_bash("!!echo interactive")
            for _ in range(3):
                await pilot.pause()
            logs = [str(c.render()) for c in app.query_one("#transcript").children]
            return started, any("Executing: !echo interactive" in l for l in logs)

    started, took_terminal_path = asyncio.run(drive())
    assert started == [], "!! must not open a card"
    assert took_terminal_path


def test_a_command_with_no_output_gets_a_small_card():
    """`code .` prints nothing; its card used to fill the whole transcript."""
    from novacode_cli.tui.widgets import OutputLog
    from textual.widgets import Static

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
    assert "(no output)" in text, text
    assert silent_h == 2, f"an empty command should be 2 rows (row + note), got {silent_h}"
    assert 10 < chatty_h <= 24, f"a long output must be capped, got {chatty_h} rows"


def test_output_is_shown_literally_and_a_failure_is_marked(tmp_path):
    """Brackets in output are text, not Rich markup; a non-zero exit shows on the row."""
    from novacode_cli.tui.widgets import OutputLog
    from textual.widgets import Static

    script = tmp_path / "fail.py"
    script.write_text("print('[bold] x [/nope]'); raise SystemExit(3)", encoding="utf-8")
    failing = f'"{sys.executable}" "{script}"'

    async def drive() -> tuple[str, list[str]]:
        app = _app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app._run_bash("!" + failing)
            await app.workers.wait_for_complete()
            for _ in range(5):
                await pilot.pause()
            block = app.query_one("#transcript").query(".bash-inline").last()
            head = str(block.query_one(".bash-inline-head", Static).render())
            return head, [strip.text for strip in block.query_one(OutputLog).lines]

    head, out = asyncio.run(drive())
    assert head.rstrip().endswith("exit 3"), head
    assert [o.rstrip() for o in out] == ["  └  [bold] x [/nope]"], out
