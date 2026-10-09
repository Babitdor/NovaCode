"""Remote subcommands typed into the prompt execute rather than opening a modal."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote.bridge import BridgeConfig, RemoteBridgeManager, RemotePlatform
from novacode_cli.tui.screens import RemoteScreen
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_prompt_authorizes_telegram_user_without_modal_or_queue(monkeypatch, busy):
    app = _app()
    config = BridgeConfig(RemotePlatform.TELEGRAM, "test", 7)
    config.allowed_user_ids = set()
    manager = RemoteBridgeManager(None)
    manager._bridges = {"telegram:7": {"config": config}}
    app.session_state._remote_bridge_manager = manager
    saved = {"telegram": {"allowed_user_ids": []}}
    monkeypatch.setattr(
        "novacode_cli.remote.config.async_load_remote_config", AsyncMock(return_value=saved)
    )
    save = AsyncMock()
    monkeypatch.setattr("novacode_cli.remote.config.async_save_remote_config", save)
    monkeypatch.setattr(app, "_ensure_remote_consumer", lambda: None)
    async with app.run_test() as pilot:
        app._turn_active = busy
        app._hide_palette()
        prompt = app.query_one("#prompt")
        app.on_input_submitted(
            SimpleNamespace(input=prompt, value="/remote allow-user telegram 6614002417")
        )
        await pilot.pause()
        assert config.allowed_user_ids == {"6614002417"}
        save.assert_awaited_once_with({"telegram": {"allowed_user_ids": ["6614002417"]}})
        assert not isinstance(app.screen, RemoteScreen)
        assert not app._deferred_commands
        app._turn_active = False


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24)])
async def test_remote_screen_authorize_and_revoke_user(monkeypatch, tmp_path, size):
    from novacode_cli.remote import config as remote_config

    monkeypatch.setattr(remote_config, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(remote_config, "_CONFIG_FILE", tmp_path / "remote.json")
    app = _app()
    config = BridgeConfig(RemotePlatform.TELEGRAM, "test", 7)
    manager = RemoteBridgeManager(None)
    manager._bridges = {"telegram:7": {"config": config}}
    app.session_state._remote_bridge_manager = manager
    monkeypatch.setattr(app, "_ensure_remote_consumer", lambda: None)
    async with app.run_test(size=size) as pilot:
        screen = RemoteScreen(app.session_state)
        await app.push_screen(screen)
        screen.query_one("#remote-user-id").value = "6614002417"
        screen.query_one("#remote-authorize").scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#remote-authorize")
        await pilot.pause()
        # UI idle does not mean the command's async configuration write finished.
        await app.workers.wait_for_complete([worker for worker in app.workers if worker.node is screen])
        await pilot.pause()
        assert config.allowed_user_ids == {"6614002417"}
        assert "6614002417" in str(screen.query_one("#remote-authorized-users").render())
        screen.query_one("#remote-revoke").scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#remote-revoke")
        await pilot.pause()
        await app.workers.wait_for_complete([worker for worker in app.workers if worker.node is screen])
        await pilot.pause()
        assert not config.allowed_user_ids
        assert remote_config.load_remote_config()["telegram"]["allowed_user_ids"] == []
