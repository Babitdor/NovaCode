"""Cross-project resume preserves ownership until the user chooses a workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.tui.resume_picker import (
    ResumeDirectoryScreen,
    ResumeModeScreen,
    ResumeSessionScreen,
)
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_resume_model import _SS, _build_app, _Meta, _patch_restore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("directory_choice", "trusted", "scope", "outcome"),
    [
        (1, True, None, "current"),
        (0, True, None, "saved"),
        (-1, True, None, "cancel"),
        (0, False, -1, "cancel"),
        (0, False, 0, "saved"),
        (0, False, 1, "saved"),
    ],
)
async def test_resume_adopts_only_the_chosen_and_approved_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_choice: int,
    *,
    trusted: bool,
    scope: int | None,
    outcome: str,
) -> None:
    from novacode_cli.config.config import settings
    from novacode_cli.path_approval import PathApprovalManager

    current, saved = tmp_path / "current", tmp_path / "saved"
    current.mkdir()
    saved.mkdir()
    monkeypatch.setattr(type(settings), "get_workspace_root", lambda _self: current)
    meta = _Meta(None, None)
    meta.project_root = str(saved)
    _patch_restore(monkeypatch, meta, (None, None))
    app = _build_app(_SS())
    save = AsyncMock()
    reopen = AsyncMock()
    monkeypatch.setattr(app, "_save_session", save)
    monkeypatch.setattr(app, "_resume_tab", reopen)
    monkeypatch.setattr(PathApprovalManager, "__init__", lambda _self: None)
    monkeypatch.setattr(PathApprovalManager, "is_path_approved", lambda _self, _path: trusted)
    approve = Mock()
    monkeypatch.setattr(PathApprovalManager, "approve_path", approve)

    async def choose(screen: object) -> int | None:
        if isinstance(screen, ResumeModeScreen):
            return 1
        if isinstance(screen, ResumeDirectoryScreen):
            return directory_choice
        return scope

    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test():
        await app._run_resume("/resume resumed-session")
        if outcome == "saved":
            reopen.assert_awaited_once_with(
                "resumed-session",
                compact_on_resume=False,
                expected_directory=saved,
            )
            save.assert_awaited_once()
            assert app.session_state.session_id == "live-session"
        elif outcome == "current":
            reopen.assert_not_awaited()
            save.assert_awaited_once()
            assert app.session_state.session_id == "resumed-session"
            assert settings.get_workspace_root() == current
        else:
            save.assert_not_awaited()
            reopen.assert_not_awaited()
            assert app.session_state.session_id == "live-session"
    if not trusted and scope in (0, 1):
        approve.assert_called_once_with(saved, recursive=scope == 0)
    else:
        approve.assert_not_called()


async def test_chat_picker_includes_other_projects_and_excludes_live_and_cleared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    app = _build_app(_SS())
    app.session_manager.list_sessions = Mock(
        return_value=[
            SimpleNamespace(session_id="live-session", project_root=str(tmp_path)),
            SimpleNamespace(session_id="other", project_root=str(tmp_path / "other")),
            SimpleNamespace(session_id="here", project_root=str(tmp_path)),
            SimpleNamespace(session_id="cleared", project_root=str(tmp_path), cleared=True),
        ]
    )
    captured = []

    async def cancel(screen: object) -> None:
        assert isinstance(screen, ResumeSessionScreen)
        captured.extend(meta.session_id for meta in screen._sessions)

    monkeypatch.setattr(app, "push_screen_wait", cancel)
    async with app.run_test():
        await app._run_resume("/resume")
    assert captured == ["other", "here"]
    assert app.session_state.session_id == "live-session"


async def test_explicit_resume_switches_to_the_existing_owner_instead_of_copying_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    app = _build_app(_SS())
    other = SimpleNamespace(
        status="running",
        state={"session_state": SimpleNamespace(session_id="other-live")},
    )
    switch = AsyncMock()
    ask = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(app, "_switch_to", switch)
    monkeypatch.setattr(app, "push_screen_wait", ask)
    monkeypatch.setattr(app, "_save_session", save)
    async with app.run_test():
        app._panes.append(other)
        try:
            await app._run_resume("/resume other-live")
        finally:
            app._panes.remove(other)
    switch.assert_awaited_once_with(other)
    ask.assert_not_awaited()
    save.assert_not_awaited()
    assert app.session_state.session_id == "live-session"
