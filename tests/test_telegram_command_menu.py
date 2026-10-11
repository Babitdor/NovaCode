"""Native Telegram slash menus match dispatch and refresh without API churn."""

import asyncio
from unittest.mock import AsyncMock

from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
from novacode_cli.remote.command_menu import build_command_menu
from novacode_cli.remote.telegram_bridge import TelegramBridge
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def bot():
    return TelegramBridge(BridgeConfig(RemotePlatform.TELEGRAM, "test-token", 7), asyncio.Queue())


def test_menu_validates_names_limits_descriptions_and_preserves_precedence():
    menu = build_command_menu(
        [
            ("help", "Help"),
            ("browser-use", "Browser"),
            ("settings", "x" * 300),
            ("bad:name", "Bad"),
            ("x" * 33, "Too long"),
            ("Ünicode", "Invalid"),
        ],
        ["help", "api-testing", "bad:name", "bad name", "review"],
    )
    assert [entry["command"] for entry in menu] == [
        "help",
        "browser_use",
        "settings",
        "api_testing",
        "review",
    ]
    assert menu[0]["description"] == "Help"
    assert len(menu[2]["description"]) == 256
    assert len(build_command_menu([("help", "Help")], [f"skill-{i}" for i in range(200)])) == 100


async def test_menu_is_chat_scoped_deduplicated_and_defensively_copied():
    bridge = bot()
    bridge._api_call = AsyncMock(return_value={"ok": True, "result": True})
    menu = build_command_menu([("help", "Help")])
    assert all(await asyncio.gather(*(bridge.sync_command_menu(menu) for _ in range(4))))
    bridge._api_call.assert_awaited_once()
    method, payload = bridge._api_call.call_args.args
    assert method == "setMyCommands"
    assert payload["scope"] == {"type": "chat", "chat_id": 7}
    assert payload["commands"] == menu
    assert bridge._api_call.call_args.kwargs["req_timeout"].total == 10
    menu[0]["description"] = "New help"
    assert await bridge.sync_command_menu(menu)
    assert bridge._api_call.await_count == 2


async def test_failed_registration_retries_and_changed_skills_replace_menu():
    bridge = bot()
    bridge._api_call = AsyncMock(side_effect=[None, {"ok": True}, {"ok": True}])
    menu = build_command_menu([("help", "Help")], ["review"])
    assert not await bridge.sync_command_menu(menu)
    assert await bridge.sync_command_menu(menu)
    changed = build_command_menu([("help", "Help")], ["test"])
    assert await bridge.sync_command_menu(changed)
    assert bridge._api_call.call_args.args[1]["commands"] == changed


async def test_app_registers_commands_plugins_panel_controls_and_enabled_skills(monkeypatch):
    app = _app()
    bridge = bot()
    bridge._api_call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(app, "_remote_telegram_bridges", lambda: [bridge])
    monkeypatch.setattr(app, "_collect_skill_names", lambda: ["review", "disabled", "api-testing"])
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: {"disabled"})
    app._plugin_commands = {"plugin-command": AsyncMock()}
    await app._sync_remote_command_menu()
    menu = {entry["command"] for entry in bridge._api_call.call_args.args[1]["commands"]}
    assert {
        "skills",
        "model",
        "choose",
        "panel",
        "tab",
        "review",
        "api_testing",
        "plugin_command",
    } <= menu
    assert "disabled" not in menu
    run = AsyncMock()
    monkeypatch.setattr(app, "_run_remote_command", run)
    for command in ("browser_use", "plugin_command"):
        reply, handler = await app._remote_slash(f"/{command}@nova_bot example")
        assert reply is None
        await handler()
    assert [call.args[0] for call in run.await_args_list] == [
        "/browser-use example",
        "/plugin-command example",
    ]


async def test_menu_skill_selection_resolves_original_skill(monkeypatch):
    app = _app()
    from types import SimpleNamespace

    resolver = AsyncMock(return_value=SimpleNamespace(pinned_prompt="run skill", prompt="unused"))
    monkeypatch.setattr("novacode_cli.commands.skill_invoke._try_skill_invocation", resolver)
    reply, prompt = await app._remote_slash("/api_testing@nova_bot file.py")
    assert reply is None and prompt == "run skill"
    assert resolver.call_args.args[:2] == ("api_testing", "file.py")


async def test_refreshes_coalesce_without_losing_changes_during_registration(monkeypatch):
    app = _app()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def refresh():
        calls.append(len(calls))
        if len(calls) == 1:
            started.set()
            await release.wait()

    monkeypatch.setattr(app, "_remote_telegram_bridges", lambda: [object()])
    monkeypatch.setattr(app, "_sync_remote_command_menu", refresh)
    monkeypatch.setattr(app, "_load_plugin_commands", lambda: None)
    async with app.run_test():
        app._schedule_remote_command_menu()
        await asyncio.wait_for(started.wait(), 5)
        first_worker = app._remote_menu_worker
        for _ in range(5):
            app._schedule_remote_command_menu()
        assert app._remote_menu_worker is first_worker
        release.set()
        await asyncio.wait_for(first_worker.wait(), 5)
        assert len(calls) == 2
