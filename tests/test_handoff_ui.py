"""Composer toggle, named previews and fresh-session continuation through real Textual pilots."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Button, Input, OptionList, Static

from novacode_cli import ui_events as ev
from novacode_cli.handoff import HandoffRun
from novacode_cli.tui.handoff_screen import HandoffScreen
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(110, 38), (55, 24)])
async def test_toggle_and_keyboard_preview_preserve_draft_and_transcript(
    size: tuple[int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    app.session_state.workspace_root = str(tmp_path)
    app.agent.astream = AsyncMock(side_effect=AssertionError("Browsing must not start work"))
    dispatch = Mock()
    monkeypatch.setattr(app, "_dispatch_root", dispatch)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "Keep my draft"
        previous_approval = app.session_state.auto_approve
        toggle = app.query_one("#handoff-toggle", Button)
        toggle.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.session_state.handoff_enabled
        assert str(toggle.label) == "Handoff: on"
        assert toggle.has_class("enabled")
        assert toggle.region.right <= size[0]
        assert app.session_state.auto_approve is previous_approval
        assert prompt.text == "Keep my draft"
        run = HandoffRun(app.session_state, "Portfolio navigation")
        run.summarize(
            "Portfolio navigation",
            "Accessible navigation",
            {
                "progress": ["Menu implemented"],
                "next_steps": ["Check mobile menu"],
                "references": ["src/menu.astro"],
                "suggested_skills": ["frontend-design"],
            },
        )
        run.finish("complete")
        before_scroll = app._transcript().scroll_y
        log = Mock(wraps=app._log)
        monkeypatch.setattr(app, "_log", log)
        await app._run_handoff("/handoff")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HandoffScreen)
        await screen.workers.wait_for_complete()
        records = screen.query_one("#handoff-list", OptionList)
        assert records.option_count == 1
        assert "Portfolio navigation" in str(records.get_option_at_index(0).prompt)
        assert run.record["id"] not in str(records.get_option_at_index(0).prompt)
        assert "Check mobile menu" in str(screen.query_one("#handoff-preview", Static).render())
        assert screen.query_one("#handoff-continue", Button).region.bottom <= size[1]
        records.focus()
        await pilot.press("enter", "pagedown")
        assert screen.focused.id == "handoff-preview-scroll"
        app.agent.astream.assert_not_called()
        dispatch.assert_not_called()
        await pilot.press("escape")
        await pilot.pause()
        assert prompt.text == "Keep my draft"
        assert app._transcript().scroll_y == before_scroll
        assert not any(call.kwargs.get("transcript") for call in log.call_args_list)


@pytest.mark.timeout(30)
async def test_continue_starts_fresh_with_current_model_permissions_and_source_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    session = app.session_state
    session.workspace_root = str(tmp_path)
    session.handoff_enabled = True
    run = HandoffRun(session, "Build portfolio")
    run.finish("cancelled")
    old_thread, old_session = session.thread_id, session.session_id
    session.plan_mode_enabled = True
    original_model = app.model_name
    original_approval = session.auto_approve
    saved = AsyncMock()
    monkeypatch.setattr(app, "_save_session", saved)
    monkeypatch.setattr(app, "_consolidate_learning", AsyncMock())
    dispatch = Mock()
    monkeypatch.setattr(app, "_dispatch_root", dispatch)
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        await app._run_handoff("/handoff")
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        await pilot.click("#handoff-continue")
        await pilot.pause()
        assert session.thread_id != old_thread
        assert session.session_id != old_session
        assert session.handoff_pending["id"] == run.record["id"]
        assert session.plan_mode_enabled
        assert session.auto_approve == original_approval
        assert app.model_name == original_model
        assert session.workspace_root == str(tmp_path)
        dispatch.assert_called_once()
        assert any(call.kwargs.get("cleared") is False for call in saved.call_args_list)


@pytest.mark.timeout(30)
async def test_empty_project_search_and_status_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = _app()
    app.session_state.workspace_root = str(tmp_path)
    async with app.run_test(size=(55, 24)) as pilot:
        await pilot.pause()
        log = Mock(wraps=app._log)
        monkeypatch.setattr(app, "_log", log)
        await app._render(ev.ContextMessage("Handoff saved", event_type="nova_handoff_status"))
        assert not any(call.kwargs.get("transcript") for call in log.call_args_list)
        await app._run_handoff("/handoff")
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        assert app.screen.query_one("#handoff-list", OptionList).option_count == 0
        assert app.screen.query_one("#handoff-continue", Button).disabled
        assert "No saved" in str(app.screen.query_one("#handoff-preview", Static).render())
        app.screen.query_one("#handoff-search", Input).value = "Nothing"
        await pilot.press("escape")
