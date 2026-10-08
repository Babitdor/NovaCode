"""Release notices stay in the transcript and open a usable changelog page."""

import asyncio
from unittest.mock import Mock

from textual.widgets import Button, Link, Static

from novacode_cli import updates
from novacode_cli.tui.update_notice import ChangelogScreen, UpdateNotice
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


async def test_available_release_notice_is_deduplicated_and_view_more_opens_changelog(monkeypatch):
    status = updates.UpdateStatus(
        True,
        "old",
        "new",
        release_version="1.0.15",
        release_title="NovaCode 1.0.15 · Responsive shells",
        release_notes="# NovaCode 1.0.15\n\n- Ctrl+B works\n",
        changelog_url="https://github.com/Babitdor/NovaCode/blob/main/changelog/CHANGELOG-v1.0.15.md",
    )
    app = _app()
    check = Mock(return_value=status)
    monkeypatch.setattr(updates, "check_for_update", check)
    monkeypatch.setattr(updates, "release_details", lambda value: value)
    async with app.run_test(size=(70, 25)) as pilot:
        monkeypatch.delenv("NOVA_DISABLE_UPDATE_CHECK")
        await asyncio.gather(app._check_nova_update(), app._check_nova_update())
        notices = list(app.query(UpdateNotice))
        assert len(notices) == 1
        assert "1.0.15" in str(notices[0].query_one(Static).content)
        notices[0].query_one(Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ChangelogScreen)
        assert "1.0.15" in str(app.screen.query_one("#modal-title", Static).content)
        assert app.screen.query_one("#changelog-link", Link).url == status.changelog_url
        await pilot.press("escape")
        assert not isinstance(app.screen, ChangelogScreen)


async def test_manual_update_screen_links_to_release_notes(monkeypatch):
    status = updates.UpdateStatus(
        True,
        "old",
        "new",
        release_version="1.0.15",
        release_title="NovaCode 1.0.15",
        changelog_url="https://github.com/Babitdor/NovaCode/tree/main/changelog",
    )
    monkeypatch.setattr(updates, "check_for_update", lambda **_kwargs: status)
    monkeypatch.setattr(updates, "release_details", lambda value: value)
    app = _app()
    async with app.run_test(size=(60, 20)) as pilot:
        await app._run_update_check("/update")
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        assert "1.0.15" in str(app.screen.query_one("#update-status", Static).content)
        button = app.screen.query_one("#update-changelog", Button)
        assert not button.disabled
        button.press()
        await pilot.pause()
        assert isinstance(app.screen, ChangelogScreen)
