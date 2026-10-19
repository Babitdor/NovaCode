"""Status preferences and explicit per-session approval controls."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from rich.text import Text
from textual.app import App
from textual.widgets import SelectionList, Static

from novacode_cli.tui.statusline import DEFAULT_FIELDS, selected_fields
from novacode_cli.tui.statusline_screen import StatusLineScreen
from tests.test_tui_sessions import _SS, _add_pane, _app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novacode_cli.config import config
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", AsyncMock())


def test_status_preferences_keep_an_empty_selection_and_ignore_unknown_fields():
    assert selected_fields(None) == DEFAULT_FIELDS
    assert selected_fields("model") == DEFAULT_FIELDS
    assert selected_fields([]) == ()
    assert selected_fields(["model", "unknown", "model"]) == ("model",)


@pytest.mark.parametrize("size", [(120, 40), (80, 24), (50, 18)])
async def test_picker_previews_keyboard_changes_and_cancel_discards(size: tuple[int, int]):
    results = []
    app = App()
    async with app.run_test(size=size) as pilot:
        screen = StatusLineScreen(["model"], lambda fields: Text(" + ".join(sorted(fields))))
        app.push_screen(screen, results.append)
        await pilot.pause()
        choices = screen.query_one(SelectionList)
        choices.highlighted = 0
        await pilot.press("space")
        assert set(choices.selected) == {"activity", "model"}
        assert "activity" in str(screen.query_one("#statusline-preview", Static).render())
        assert screen.region.contains_region(screen.query_one("#statusline-cancel").region)
        await pilot.press("escape")
        assert results == [None]


async def test_saved_fields_drive_the_bar_and_survive_restart(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.nova_config import NovaConfig

    app = _app()
    async with app.run_test():
        app._set_info("#info-branch", Text("feature/status"))
        monkeypatch.setattr(app, "push_screen_wait", AsyncMock(return_value=["model", "branch"]))
        await app._run_statusline("/statusline")
        assert NovaConfig().get("statusline_fields") == ["model", "branch"]
        left, right = app._build_status_tail()
        assert "branch: feature/status" in left.plain
        assert "CTX" not in left.plain
        assert not right
        assert _app()._statusline_fields == ("model", "branch")
        monkeypatch.setattr(app, "push_screen_wait", AsyncMock(return_value=None))
        await app._run_statusline("/statusline")
        assert app._statusline_fields == ("model", "branch")


@pytest.mark.parametrize("design", ["classic", "dashboard", "minimal", "banner", "centered"])
async def test_auto_approve_toggles_composer_and_never_persists(design: str):
    from novacode_cli.config.nova_config import NovaConfig

    app = _app()
    app.session_state.auto_approve = False
    async with app.run_test() as pilot:
        app.screen.add_class(f"{design}-ui")
        before = app.query_one("#prompt").styles.background
        await app._run_slash("/auto-approve on")
        await pilot.pause()
        row = app.query_one("#prompt-row")
        assert app.session_state.auto_approve
        assert row.has_class("auto-approve-mode")
        assert app.query_one("#prompt").has_class("auto-approve-mode")
        assert app.query_one("#prompt").styles.background != before
        assert "AUTO-APPROVE" in str(app.query_one("#mode-badge", Static).render())
        assert NovaConfig().get("auto_approve") is None
        await app._run_slash("/auto-approve off")
        assert not app.session_state.auto_approve
        assert not row.has_class("auto-approve-mode")


async def test_child_toggle_targets_worker_and_composer_follows_tab_switch():
    from novacode_cli.tui.session_pane import fresh_state

    app = _app()
    app.session_state.auto_approve = False
    async with app.run_test():
        child = await _add_pane(app)
        child.state = fresh_state(session_state=_SS())
        child.state["session_state"].auto_approve = False
        supervisor = AsyncMock()
        supervisor.set_auto_approve.return_value = True
        app._session_supervisor = supervisor
        await app._switch_to(child)
        await app._dispatch_to_child(child, "/auto-approve on")
        supervisor.set_auto_approve.assert_awaited_once_with(child.sid, enabled=True)
        assert app.query_one("#prompt").has_class("auto-approve-mode")
        # Busy input takes the native slash handler, not _dispatch_to_child.
        child.status = "running"
        await app._run_slash("/auto-approve off")
        supervisor.set_auto_approve.assert_awaited_with(child.sid, enabled=False)
        assert not child.state["session_state"].auto_approve
        await app._switch_to(app._root_pane)
        assert not app.session_state.auto_approve
        assert not app.query_one("#prompt").has_class("auto-approve-mode")


async def test_remote_toggle_and_invalid_input_keep_explicit_session_preference():
    app = _app()
    app.session_state.auto_approve = False
    app.session_state._pre_remote_auto_approve = False
    async with app.run_test():
        reply, action = await app._remote_slash("/auto_approve on")
        assert reply == "Auto-approve on for this session."
        assert action is None
        assert app.session_state.auto_approve
        assert app.session_state._pre_remote_auto_approve
        reply, _ = await app._remote_slash("/auto-approve invalid")
        assert reply.startswith("Usage:")
        assert app.session_state.auto_approve
        await app._run_slash("/auto-approve")
        assert not app.session_state.auto_approve


async def test_picker_save_returns_the_draft_and_reset_does_not_apply_it():
    results = []
    app = App()
    async with app.run_test() as pilot:
        screen = StatusLineScreen(["model"], lambda fields: Text(" + ".join(sorted(fields))))
        app.push_screen(screen, results.append)
        await pilot.pause()
        await pilot.click("#statusline-reset")
        assert set(screen.query_one(SelectionList).selected) == set(DEFAULT_FIELDS)
        assert not results
        await pilot.click("#statusline-save")
        assert set(results[0]) == set(DEFAULT_FIELDS)


async def test_effort_rebuild_keeps_the_active_provider_model_and_shared_steering(
    monkeypatch: pytest.MonkeyPatch,
):
    from types import SimpleNamespace

    from novacode_cli.config import model_create
    from novacode_cli.states.Session import SessionState

    app = _app()
    app.session_state = SessionState(no_splash=True)
    app._model_provider = "commandcode"
    app.model_name = "deepseek/deepseek-v4-flash"
    live_model = SimpleNamespace(model_name=app.model_name)
    app.session_state._model = live_model
    app.session_state.steering_instructions = []
    runtime_switch = AsyncMock(return_value=(app.agent, app.backend))
    monkeypatch.setattr(app.session_state._agent_runtime, "switch_model", runtime_switch)
    builds = []

    def build(provider: str, name: str) -> SimpleNamespace:
        builds.append((provider, name))
        return live_model

    monkeypatch.setattr(model_create, "build_chat_model", build)
    async with app.run_test():
        await app._run_effort("/effort high")
        assert builds == [("commandcode", "deepseek/deepseek-v4-flash")]
        runtime_switch.assert_awaited_once_with(
            live_model,
            steering_instructions=app.session_state.steering_instructions,
            session_id=app.session_state.session_id,
        )
        assert app._reasoning_effort == "high"
