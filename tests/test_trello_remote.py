"""Board toggle, isolated Telegram intake, duplicates, and failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from novacode_cli.commands.trello_server import TrelloServer
from novacode_cli.commands.trello_remote import connect_telegram
from novacode_cli.remote.bridge import BridgeConfig, RemoteMessage, RemotePlatform
from novacode_cli.remote.telegram_bridge import TelegramBridge


def message(text="Fix tests", *, topic=55, mid=1, chat=-100123):
    return RemoteMessage(
        RemotePlatform.TELEGRAM,
        chat,
        "Alice",
        text,
        AsyncMock(),
        sender_id=123,
        thread_id=topic,
        message_id=mid,
    )


async def drain(server):
    await asyncio.sleep(0)
    controller = server._remote_controller
    while controller._tasks:
        await asyncio.gather(*list(controller._tasks))


async def connected():
    server = TrelloServer()
    server.is_running = True
    bot = TelegramBridge(
        BridgeConfig(RemotePlatform.TELEGRAM, "secret", -100123, allowed_user_ids={123}),
        asyncio.Queue(),
    )
    bot._running, bot.bot_user = True, "nova"
    bot.ensure_session_topic = AsyncMock(return_value=55)
    bot.post = AsyncMock()
    server.bind_remote(AsyncMock(return_value=bot), "board-1")
    server.request_remote(True)
    await drain(server)
    return server, bot


@pytest.mark.asyncio
async def test_toggle_creates_topic_and_messages_become_loaded_cards_once():
    server, bot = await connected()
    assert server.get_state()["remote"] == {
        "enabled": True,
        "status": "connected",
        "topic_id": 55,
        "topic_url": "https://t.me/c/123/55",
        "error": None,
    }
    bot.ensure_session_topic.assert_awaited_once_with(
        "trello:board-1", "trello-board-1", "Trello board", verify=True
    )
    server.request_remote(True)
    await drain(server)
    assert bot.ensure_session_topic.await_count == 1
    msg = message()
    assert await bot._dispatch_topic_message(msg)
    assert await bot._dispatch_topic_message(msg)
    cards = server.get_tasks()
    assert len(cards) == 1
    assert cards[0]["status"] == "loaded"
    assert cards[0]["description"] == "Fix tests"
    assert cards[0]["source"]["sender"] == "Alice"
    assert bot._queue.empty()
    server.request_remote(False)
    await drain(server)
    assert await bot._dispatch_topic_message(message("Off task", mid=2))
    assert len(server.get_tasks()) == 1
    server.request_remote(True)
    await drain(server)
    assert await bot._dispatch_topic_message(message("Next task", mid=2))
    assert len(server.get_tasks()) == 2


@pytest.mark.asyncio
async def test_wrong_topic_or_chat_and_commands_do_not_create_cards():
    server, bot = await connected()
    assert not await bot._dispatch_topic_message(message(topic=66))
    assert not await bot._dispatch_topic_message(message(chat=-100999))
    for index, text in enumerate(("/tab", "/help", "/status", "/add", "/add@nova   Build UI")):
        msg = message(text, mid=index + 10)
        assert await bot._dispatch_topic_message(msg)
        msg.reply_fn.assert_awaited_once()
    assert [task["description"] for task in server.get_tasks()] == ["Build UI"]
    server.is_running = False
    assert await bot._dispatch_topic_message(message("Stopped task", mid=77))
    assert len(server.get_tasks()) == 1


@pytest.mark.asyncio
async def test_failed_connection_does_not_leak_credentials_or_stay_enabled():
    server = TrelloServer()
    server.is_running = True
    server.bind_remote(AsyncMock(side_effect=ValueError("secret-token-in-url")), "board")
    server.request_remote(True)
    await drain(server)
    state = server.get_state()["remote"]
    assert state["status"] == "error" and not state["enabled"]
    assert "secret-token" not in str(state)


@pytest.mark.asyncio
async def test_turning_off_during_connection_wins():
    server = TrelloServer()
    server.is_running = True
    gate, entered = asyncio.Event(), asyncio.Event()

    async def connect():
        entered.set()
        await gate.wait()
        raise RuntimeError("connection failed late")

    server.bind_remote(connect, "board")
    server.request_remote(True)
    await entered.wait()
    server.request_remote(False)
    gate.set()
    await drain(server)
    assert server.get_state()["remote"]["status"] == "off"
    assert not server.remote_requested


@pytest.mark.asyncio
async def test_existing_connection_is_reused_without_reading_credentials(monkeypatch):
    bot = object()
    manager = SimpleNamespace(bridges=lambda platform: [bot])
    read = AsyncMock(side_effect=AssertionError("No credential read needed"))
    monkeypatch.setattr("novacode_cli.remote.config.async_get_telegram_config", read)
    state = SimpleNamespace(_remote_bridge_manager=manager, auto_approve=False)
    assert await connect_telegram(state) is bot
    assert state.auto_approve is False
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_toggle_is_authenticated_and_preserves_auto_advance():
    server = TrelloServer()
    await server.start()
    bot = SimpleNamespace(
        ensure_session_topic=AsyncMock(return_value=55),
        post=AsyncMock(),
        set_topic_handler=lambda *args: None,
        _config=SimpleNamespace(chat_id=-100123),
        is_connected=True,
    )
    server.bind_remote(AsyncMock(return_value=bot), "board")
    server.set_auto_advance(True)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            url = f"http://localhost:{server.port}/api/settings"
            headers = {"X-Nova-Request": "1", "X-Nova-Token": server._server.auth_token}
            # Authentication rejects before reading a POST body; Windows can
            # reset a socket with unread data after the response headers.
            async with client.stream("POST", url, json={"telegram_enabled": True}) as response:
                assert response.status_code == 403
            assert not server.remote_requested
            assert (
                await client.post(url, json={"telegram_enabled": "true"}, headers=headers)
            ).status_code == 400
            assert (
                await client.post(url, json={"telegram_enabled": True}, headers=headers)
            ).status_code == 200
            await drain(server)
            assert server.auto_advance
            assert server.get_state()["remote"]["status"] == "connected"
    finally:
        await asyncio.to_thread(server.stop)
        await drain(server)


@pytest.mark.asyncio
async def test_authorization_precedes_topic_intake_and_session_messages_still_queue():
    server, bot = await connected()
    bot._api_call = AsyncMock(return_value={"result": {"username": "nova"}})

    def update(mid, user, topic):
        return {
            "update_id": mid,
            "message": {
                "message_id": mid,
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": user},
                "message_thread_id": topic,
                "is_topic_message": True,
                "text": "Fix tests",
            },
        }

    bot._get_updates = AsyncMock(
        side_effect=[
            [update(1, 999, 55), update(2, 123, 55), update(3, 123, 66)],
            asyncio.CancelledError(),
        ]
    )
    await bot.run()
    assert len(server.get_tasks()) == 1
    assert bot._queue.qsize() == 1
    assert bot._queue.get_nowait().thread_id == 66


@pytest.mark.asyncio
async def test_intake_error_is_consumed_and_never_dispatched_to_agent():
    server, bot = await connected()
    bot.set_topic_handler(55, AsyncMock(side_effect=RuntimeError("storage failed")))
    assert await bot._dispatch_topic_message(message())
    assert bot._queue.empty()
    assert server.get_tasks() == []


@pytest.mark.asyncio
async def test_deleted_cached_board_topic_recovers_and_keeps_intake_bindings(monkeypatch):
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    bot = TelegramBridge(BridgeConfig(RemotePlatform.TELEGRAM, "test", -100123), asyncio.Queue())
    bot._shared_polling = SimpleNamespace(reserve_session=AsyncMock(), subscribe=AsyncMock())
    bot.open_topic = AsyncMock(side_effect=[55, 77, 88])
    assert await bot.ensure_session_topic("trello:board", "trello-board", "Trello") == 55
    bot.set_topic_handler(55, AsyncMock())
    bot._api_call = AsyncMock(
        return_value={
            "ok": False,
            "error_code": 400,
            "description": "TOPIC_ID_INVALID",
        }
    )
    assert (
        await bot.ensure_session_topic("trello:board", "trello-board", "Trello", verify=True) == 77
    )
    bot.set_topic_handler(77, AsyncMock())
    assert await bot.ensure_session_topic("root", "session-root", "main") == 88
    bindings = bot._shared_polling.subscribe.call_args.args[0]
    assert (-100123, 55) in bindings  # old intake remains guarded
    assert (-100123, 77) in bindings
    assert (-100123, 88) in bindings


@pytest.mark.asyncio
async def test_saved_connection_starts_without_enabling_auto_approve(monkeypatch):
    saved = {"token": "protected-token", "chat_id": "-100123"}
    monkeypatch.setattr(
        "novacode_cli.remote.config.async_get_telegram_config", AsyncMock(return_value=saved)
    )
    bot = SimpleNamespace(_config=SimpleNamespace(chat_id=-100123))
    manager = SimpleNamespace(start_telegram=AsyncMock(return_value=(True, "")))
    count = 0

    def bridges(platform):
        nonlocal count
        count += 1
        return [] if count == 1 else [bot]

    manager.bridges = bridges
    state = SimpleNamespace(_remote_bridge_manager=manager, auto_approve=False)
    assert await connect_telegram(state) is bot
    manager.start_telegram.assert_awaited_once_with(token="protected-token", chat_id=-100123)
    assert state.auto_approve is False


@pytest.mark.asyncio
async def test_acknowledgment_failure_does_not_duplicate_task():
    server, bot = await connected()
    msg = message()
    msg.reply_fn = AsyncMock(side_effect=[RuntimeError("delivery failed"), None, None])
    assert await bot._dispatch_topic_message(msg)
    assert await bot._dispatch_topic_message(msg)
    assert len(server.get_tasks()) == 1


@pytest.mark.asyncio
async def test_board_execution_waits_for_owner_tab_and_logs_stay_there(monkeypatch, tmp_path):
    from tests.test_tui_sessions import _add_pane, _app, isolated_session_config

    # Apply the same isolated configuration as the tab regression suite.
    isolated_session_config.__wrapped__(monkeypatch, tmp_path)
    from novacode_cli.tui.session_pane import fresh_state

    app = _app()
    async with app.run_test() as pilot:
        root, agent, state, tracker = (
            app._root_pane,
            app.agent,
            app.session_state,
            app.token_tracker,
        )
        child = await _add_pane(app)
        child.state = fresh_state()
        await app._switch_to(child)
        server = TrelloServer()
        server.is_running = True
        card = server.add_task("Board task")
        server.move_task(card["id"], "processing")
        executed = []

        async def execute(prompt, **kwargs):
            executed.append((app._active_pane, prompt))
            server.is_running = False

        monkeypatch.setattr(app, "_tui_execute_fn", execute)
        watching = asyncio.create_task(
            app._trello_watch_loop(
                server,
                owner_pane=root,
                agent=agent,
                assistant_id="nova-agent",
                session_state=state,
                token_tracker=tracker,
            )
        )
        try:
            await asyncio.sleep(0.15)
            assert not executed
            assert root.buffer and not child.buffer
            await app._switch_to(root)
            await asyncio.wait_for(watching, 3)
            assert executed == [(root, "Board task")]
            await pilot.pause()
        finally:
            server.is_running = False
            watching.cancel()
