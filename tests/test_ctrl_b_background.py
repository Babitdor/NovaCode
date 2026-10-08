"""Ctrl+B reaches running commands through the focused editor."""

import asyncio
import sys
from unittest.mock import Mock

from textual.widgets import Static

from novacode_cli.shell import jobs
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


async def test_focused_prompt_ctrl_b_detaches_shell_and_preserves_draft(monkeypatch):
    app = _app()
    ctl = jobs.set_current("running execute")
    try:
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "follow-up draft"
            # Editors / keymaps can assign Ctrl+B to cursor movement. The app
            # shortcut must still win when that binding is present.
            prompt._bindings.bind("ctrl+b", "cursor_left")
            prompt.focus()
            panel = Mock()
            monkeypatch.setattr(app, "_open_tasks_panel", panel)
            await pilot.press("ctrl+b")
            assert ctl.detach.is_set()
            assert not ctl.kill.is_set()
            assert prompt.value == "follow-up draft"
            panel.assert_not_called()
    finally:
        jobs.clear_current(ctl)


async def test_bang_command_background_handoff_keeps_process_and_finishes(tmp_path):
    script = tmp_path / "command.py"
    script.write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(1)\nprint('finished', flush=True)\n"
    )
    app = _app()
    async with app.run_test() as pilot:
        await app._run_bash(f'!"{sys.executable}" "{script}"')
        for _ in range(100):
            if jobs.get_current() is not None:
                break
            await asyncio.sleep(0.01)
        assert jobs.get_current() is not None
        hint = app.query_one(".bash-inline .background-shell-hint", Static)
        assert hint.display and "Ctrl+B" in str(hint.content)
        await pilot.press("ctrl+b")
        for _ in range(100):
            if jobs.get_registry().active():
                break
            await asyncio.sleep(0.01)
        active = jobs.get_registry().active()
        assert len(active) == 1
        pid = active[0].pid
        assert pid is not None and jobs.get_current() is None
        assert not hint.display
        await app.workers.wait_for_complete()
        assert active[0].pid == pid
        assert active[0].status == "done"
        assert "finished" in active[0].output
        assert jobs.get_registry().active_count() == 0


async def test_execute_handoff_survives_turn_cancellation_and_retires_control(monkeypatch, tmp_path):
    from novacode_cli.shell.middleware import ShellMiddleware

    app = _app()
    middleware = ShellMiddleware(workspace_root=".", timeout=30)
    release = tmp_path / "release-command"
    command = (
        "import time; from pathlib import Path; print('started', flush=True)\n"
        f"release = Path({str(release)!r})\n"
        "while not release.exists(): time.sleep(0.01)\n"
        "print('finished', flush=True)"
    )
    async with app.run_test() as pilot:
        monkeypatch.setattr(app, "_continue_after_task", lambda _job: None)
        app._turn_active = True
        worker = app.run_worker(
            middleware._adispatch_local(
                command,
                tool_call_id="execute-test",
                prog=[sys.executable, "-u", "-c"],
            ),
            group="turn",
        )
        for _ in range(100):
            if jobs.get_current() is not None:
                break
            await asyncio.sleep(0.01)
        assert jobs.get_current() is not None
        await pilot.press("ctrl+b")
        for _ in range(100):
            if jobs.get_registry().active():
                break
            await asyncio.sleep(0.01)
        active = jobs.get_registry().active()
        assert len(active) == 1 and worker.is_cancelled
        assert jobs.get_current() is None
        assert jobs.request_kill() is False, "Esc must not kill a background command"
        app._turn_active = False
        release.touch()
        for _ in range(200):
            if active[0].status != "running":
                break
            await asyncio.sleep(0.01)
        assert active[0].status == "done"
        assert "finished" in active[0].output


async def test_running_execute_hint_hides_when_tool_finishes():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test():
        await app._render(
            ev.ToolCall(name="execute", display_str="pytest", icon="$", call_id="command")
        )
        hint = app.query_one("#tool-group-background-hint", Static)
        assert hint.display and "Ctrl+B" in str(hint.content)
        await app._render(
            ev.ToolResult(call_id="command", preview="Done", full_output="OK", is_error=False)
        )
        assert not hint.display


async def test_interrupted_tool_group_hides_background_hint():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test():
        await app._render(ev.ToolCall(name="bash", display_str="sleep", icon="$", call_id="c"))
        hint = app.query_one("#tool-group-background-hint", Static)
        assert hint.display
        app._close_tool_group()
        assert not hint.display


async def test_detached_inline_command_can_be_killed_from_registry(tmp_path):
    script = tmp_path / "long_command.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n")
    app = _app()
    async with app.run_test() as pilot:
        await app._run_bash(f'!"{sys.executable}" "{script}"')
        for _ in range(100):
            if jobs.get_current() is not None:
                break
            await asyncio.sleep(0.01)
        assert jobs.get_current() is not None
        await pilot.press("ctrl+b")
        for _ in range(100):
            if jobs.get_registry().active():
                break
            await asyncio.sleep(0.01)
        active = jobs.get_registry().active()
        assert len(active) == 1
        active[0].kill.set()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10)
        assert active[0].status == "terminated"
        assert jobs.get_registry().active_count() == 0
        assert jobs.get_current() is None


async def test_repeated_ctrl_b_opens_only_one_tasks_panel():
    from novacode_cli.tui.screens import BackgroundTasksScreen

    app = _app()
    async with app.run_test() as pilot:
        await pilot.press(*(["ctrl+b"] * 8))
        assert sum(isinstance(screen, BackgroundTasksScreen) for screen in app.screen_stack) == 1
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        assert not any(isinstance(screen, BackgroundTasksScreen) for screen in app.screen_stack)
        await pilot.press("ctrl+b")
        assert isinstance(app.screen, BackgroundTasksScreen)
        await pilot.press("escape")


async def test_repeated_ctrl_b_during_handoff_never_submits_preserved_draft():
    app = _app()
    ctl = jobs.set_current("pending handoff")
    try:
        async with app.run_test():
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "my unsubmitted draft"
            count = app._bg_job_count
            for _ in range(8):
                await app.action_run_background()
            assert ctl.detach.is_set()
            assert prompt.value == "my unsubmitted draft"
            assert app._bg_job_count == count
    finally:
        jobs.clear_current(ctl)
