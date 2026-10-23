"""Rootapp setup and sender controls are reachable from the native remote UI."""

import pytest
from textual.widgets import Select

from novacode_cli.tui.screens import RemoteScreen
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.mark.parametrize("size", [(100, 40), (60, 24)])
async def test_rootapp_fields_start_and_sender_access(monkeypatch, tmp_path, size):
    from novacode_cli.remote import config

    monkeypatch.setattr(config, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "_CONFIG_FILE", tmp_path / "remote.json")
    app = _app()
    calls = []
    async with app.run_test(size=size) as pilot:
        screen = RemoteScreen(app.session_state)
        await app.push_screen(screen)
        await pilot.pause()
        monkeypatch.setattr(screen, "_run_remote", calls.append)
        screen.query_one("#remote-token").value = "dev-secret"
        screen.query_one("#remote-chat").value = "channel-1"
        screen.query_one("#rootapp-bot-id").value = "bot-1"
        assert (
            screen._build_args("rootapp")
            == "start rootapp --token dev-secret --channel channel-1 --bot-id bot-1"
        )
        assert screen.query_one("#remote-token").password
        screen.query_one("#start-rootapp").press()
        screen.query_one("#setup-rootapp").press()
        await pilot.pause()
        assert calls[:2] == [screen._build_args("rootapp"), "setup rootapp"]
        screen.query_one("#remote-access-platform", Select).value = "rootapp"
        screen.query_one("#remote-user-id").value = "root-user-guid"
        await pilot.pause()
        screen.query_one("#remote-authorize").press()
        await pilot.pause()
        assert calls[-1] == "allow-user rootapp root-user-guid"
        assert "Authorized Rootapp users" in str(
            screen.query_one("#remote-authorized-users").render()
        )
