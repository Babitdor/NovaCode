"""Transient feedback stays out of the transcript; requested output remains readable."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from rich.text import Text

from novacode_cli import ui_events as ev
from novacode_cli.remote.command_ui import command_output
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, no_system_sampling  # noqa: F401


@pytest.mark.timeout(30)
async def test_notices_preserve_transcript_draft_scroll_and_tool_group(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        toast = Mock()
        close_group = Mock()
        monkeypatch.setattr(app, "notify", toast)
        monkeypatch.setattr(app, "_close_tool_group", close_group)
        transcript = app._transcript()
        before = list(transcript.children)
        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "Keep my draft"
        app._follow_tail = False
        original_scroll = transcript.scroll_y
        app._log(Text("Saved [profile]", style="green"))
        await pilot.pause()
        toast.assert_called_once_with("Saved [profile]", timeout=4, markup=False)
        app._log("Saved [literal text]")
        toast.assert_called_with("Saved [literal text]", timeout=4, markup=False)
        close_group.assert_not_called()
        assert list(transcript.children) == before
        assert prompt.text == "Keep my draft"
        assert transcript.scroll_y == original_scroll
        assert not app._follow_tail
        # Remote command capture must keep feedback even though the TUI uses a toast.
        captured = []
        token = command_output.set(captured)
        try:
            app._log(Text("Remote confirmation"))
        finally:
            command_output.reset(token)
        assert captured[0].plain == "Remote confirmation"
        assert list(transcript.children) == before
        app._log(Text("Requested report\nFull details"), transcript=True)
        await pilot.pause()
        close_group.assert_called_once()
        assert len(transcript.children) == len(before) + 1
        assert "Full details" in str(transcript.children[-1].render())


@pytest.mark.timeout(30)
async def test_progress_uses_toasts_but_reports_and_conversation_remain(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        toast = Mock()
        monkeypatch.setattr(app, "notify", toast)
        transcript = app._transcript()
        before = list(transcript.children)
        await app._render(ev.ContextMessage("Investigating", event_type="nova_prime_progress"))
        await app._render(
            ev.ContextMessage(
                "Prime Deep Work · delivered\nDetailed assessment",
                event_type="nova_prime_receipt",
            )
        )
        await pilot.pause()
        assert list(transcript.children) == before
        assert toast.call_args.args[0] == (
            "Prime Deep Work · delivered. Use /prime report for details."
        )
        await app._run_prime("/prime status")
        await app._run_plan("/plan status")
        await app._render(ev.ContextMessage("Command output", event_type="nova_command_output"))
        await app._render(ev.AssistantMessage("Agent answer", "Nova", "cyan"))
        await pilot.pause()
        assert len(transcript.children) >= len(before) + 4
        assert any(
            "Agent answer" in getattr(widget, "raw_text", "") for widget in transcript.children
        )


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(110, 38), (55, 24)])
async def test_native_notice_appears_then_expires(size: tuple[int, int]):
    app = _app()
    async with app.run_test(size=size, notifications=True) as pilot:
        await pilot.pause()
        before = list(app._transcript().children)
        app._log(Text("Settings saved [literal text]"))
        async with asyncio.timeout(3):
            while not app.query("Toast"):
                await pilot.pause(0.01)
        await pilot.pause()
        toast = app.query_one("Toast")
        assert 0 <= toast.region.x < toast.region.right <= size[0]
        assert 0 <= toast.region.y < toast.region.bottom <= size[1]
        assert list(app._transcript().children) == before
        async with asyncio.timeout(6):
            while app.query("Toast"):
                await pilot.pause(0.05)
        assert list(app._transcript().children) == before
