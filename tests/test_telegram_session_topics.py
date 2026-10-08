"""Topics attach stable session IDs to independent Telegram conversations."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
from novacode_cli.remote.telegram_bridge import TelegramBridge


def bridge():
    return TelegramBridge(BridgeConfig(RemotePlatform.TELEGRAM, "test-token", 7), asyncio.Queue())


@pytest.mark.asyncio
async def test_topic_probe_can_read_telegram_error_response(monkeypatch):
    bot = bridge()
    error = {"ok": False, "error_code": 400, "description": "Bad Request: TOPIC_ID_INVALID"}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return error

    monkeypatch.setattr(
        bot, "_ensure_session", lambda: SimpleNamespace(post=lambda *args, **kwargs: Response())
    )
    assert await bot._api_call("reopenForumTopic", {}, return_errors=True) == error
    assert await bot._api_call("sendMessage", {}) is None


@pytest.mark.asyncio
async def test_topic_creation_is_idempotent_concurrent_and_durable(monkeypatch):
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    bot = bridge()
    bot.open_topic = AsyncMock(return_value=55)
    results = await asyncio.gather(
        *(bot.ensure_session_topic("root", "session-alpha", "main") for _ in range(4))
    )
    assert results == [55] * 4
    bot.open_topic.assert_awaited_once()
    assert "session-alpha" in bot.open_topic.call_args.args[0]
    restarted = bridge()
    restarted._api_call = AsyncMock(return_value={"ok": True, "result": True})
    restarted.open_topic = AsyncMock(return_value=99)
    assert await restarted.ensure_session_topic("root", "session-alpha", "main") == 55
    restarted.open_topic.assert_not_awaited()
    assert await restarted.ensure_session_topic("child", "session-beta", "beta") == 99


@pytest.mark.asyncio
async def test_local_stream_uses_its_session_topic(monkeypatch):
    bot = bridge()
    bot._running, bot.bot_user = True, "nova"
    bot._session_topics = {"root": ("session-alpha", 55), "child": ("session-beta", 66)}
    bot._send_message = AsyncMock()
    targets = [await bot.stream_target(sid=sid) for sid in ("root", "child")]
    await targets[0].reply_fn("alpha")
    await targets[1].reply_fn("beta")
    assert [call.kwargs["thread_id"] for call in bot._send_message.call_args_list] == [55, 66]
    assert [target.route["thread"] for target in targets] == [55, 66]


@pytest.mark.asyncio
async def test_private_topic_mode_and_transient_detection(monkeypatch):
    bot = bridge()
    bot._api_call = AsyncMock(
        side_effect=[
            None,
            {"result": {"type": "private"}},
            {"result": {"has_topics_enabled": True}},
        ]
    )
    assert not await bot.detect_forum()
    assert await bot.detect_forum()


@pytest.mark.asyncio
async def test_tui_attaches_existing_root_and_child_without_duplicates(monkeypatch):
    from novacode_cli.remote.routing import RemoteRouter
    from novacode_cli.tui.app import NovaApp

    bot = SimpleNamespace(
        _config=SimpleNamespace(chat_id=7),
        ensure_session_topic=AsyncMock(side_effect=[55, 66, 55, 66]),
        post=AsyncMock(),
    )
    app = NovaApp.__new__(NovaApp)
    root = SimpleNamespace(sid="root", title="main", kind="root", state={})
    child = SimpleNamespace(sid="child-id", title="beta", kind="child", status="idle", state={})
    app._root_pane = app._active_pane = root
    app._panes = [root, child]
    app.session_state = SimpleNamespace(session_id="session-alpha")
    app._remote_router = RemoteRouter()
    monkeypatch.setattr(app, "_remote_telegram_bridges", lambda: [bot])
    await app._sync_remote_topics()
    await app._sync_remote_topics()
    assert app._remote_router.topics == {(7, "root"): 55, (7, "child-id"): 66}
    assert bot.post.await_count == 2
    assert bot.ensure_session_topic.call_args_list[0].args == ("root", "session-alpha", "main")


@pytest.mark.asyncio
async def test_closed_session_topic_reopens_instead_of_creating_another(monkeypatch):
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    bot = bridge()
    bot.open_topic = AsyncMock(return_value=55)
    bot._api_call = AsyncMock(return_value={"ok": True, "result": True})
    assert await bot.ensure_session_topic("child", "session-alpha", "alpha") == 55
    await bot.close_topic(55)
    assert not bot._session_topics
    assert await bot.ensure_session_topic("child", "session-alpha", "alpha") == 55
    bot.open_topic.assert_awaited_once()
    assert bot._api_call.call_args.args[0] == "reopenForumTopic"


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_deleted_saved_topic_is_recreated_and_rebound(monkeypatch, closed):
    import hashlib

    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    namespace = ("nova", "telegram_topics", hashlib.sha256(b"test-token").hexdigest(), "7")
    await store.aput(namespace, "session-alpha", {"topic": 55, "closed": closed})
    bot = bridge()
    bot._api_call = AsyncMock(
        return_value={
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: TOPIC_ID_INVALID",
        }
    )
    bot.open_topic = AsyncMock(return_value=99)
    bot._shared_polling = SimpleNamespace(reserve_session=AsyncMock(), subscribe=AsyncMock())
    assert await bot.ensure_session_topic("root", "session-alpha", "main") == 99
    assert bot._session_topics == {"root": ("session-alpha", 99)}
    assert (await store.aget(namespace, "session-alpha")).value == {"topic": 99}
    bot._shared_polling.subscribe.assert_awaited_once_with([(7, 0), (7, 99)])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {"ok": False, "error_code": 403, "description": "Forbidden"},
        {"ok": False, "error_code": 429, "description": "Too Many Requests"},
        {"ok": False, "error_code": 500, "description": "Internal Server Error"},
    ],
)
async def test_topic_probe_failure_does_not_create_duplicate(monkeypatch, response):
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    bot = bridge()
    bot.open_topic = AsyncMock(return_value=55)
    await bot.ensure_session_topic("root", "session-alpha", "main")
    restarted = bridge()
    restarted.open_topic = AsyncMock(return_value=99)
    restarted._api_call = AsyncMock(return_value=response)
    with pytest.raises(RuntimeError, match="verify"):
        await restarted.ensure_session_topic("root", "session-alpha", "main")
    restarted.open_topic.assert_not_awaited()
    assert not restarted._session_topics


@pytest.mark.asyncio
async def test_already_open_saved_topic_is_reused(monkeypatch):
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    bot = bridge()
    bot.open_topic = AsyncMock(return_value=55)
    await bot.ensure_session_topic("root", "session-alpha", "main")
    restarted = bridge()
    restarted.open_topic = AsyncMock(return_value=99)
    restarted._api_call = AsyncMock(
        return_value={
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: TOPIC_NOT_MODIFIED",
        }
    )
    assert await restarted.ensure_session_topic("root", "session-alpha", "main") == 55
    restarted.open_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_received_registered_topic_without_flag_keeps_routing_and_reply():
    from novacode_cli.remote.routing import RemoteRouter

    bot = bridge()
    bot._session_topics = {"root": ("session-alpha", 55)}
    bot._api_call = AsyncMock(return_value={"result": {"username": "nova"}})
    bot._get_updates = AsyncMock(
        side_effect=[
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 123},
                        "message_thread_id": 55,
                        "text": "fix the bug",
                    },
                }
            ],
            asyncio.CancelledError(),
        ]
    )
    bot._send_message = AsyncMock()
    await bot.run()
    msg = bot._queue.get_nowait()
    assert msg.thread_id == 55
    router = RemoteRouter(topics={(7, "root"): 55})
    assert router.resolve(msg, {"root": "main"}).via == "topic"
    await msg.reply_fn("fixed")
    assert bot._send_message.call_args.kwargs["thread_id"] == 55


@pytest.mark.asyncio
async def test_group_sender_block_is_visible_locally_and_still_rejected():
    bot = bridge()
    bot._config.allowed_user_ids = set()
    bot._on_status = AsyncMock()
    bot._api_call = AsyncMock(return_value={"result": {"username": "nova"}})
    bot._get_updates = AsyncMock(
        side_effect=[
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 7, "type": "supergroup"},
                        "from": {"id": 123},
                        "message_thread_id": 55,
                        "text": "fix the bug",
                    },
                }
            ],
            asyncio.CancelledError(),
        ]
    )
    await bot.run()
    assert bot._queue.empty()
    bot._on_status.assert_awaited_once()
    assert "/remote allow-user telegram 123" in bot._on_status.call_args.args[0]


@pytest.mark.asyncio
async def test_local_sender_authorization_persists_updates_bridge_and_can_be_revoked(monkeypatch):
    from novacode_cli.commands.commands import _handle_remote_command

    bot = bridge()
    saved = {"telegram": {"token": "keep-token", "chat_id": "7", "allowed_user_ids": []}}
    monkeypatch.setattr(
        "novacode_cli.remote.config.async_load_remote_config", AsyncMock(return_value=saved)
    )
    save = AsyncMock()
    monkeypatch.setattr("novacode_cli.remote.config.async_save_remote_config", save)
    state = SimpleNamespace(
        _remote_bridge_manager=SimpleNamespace(_bridges={"tg": {"config": bot._config}})
    )
    console = SimpleNamespace(print=lambda *args: None)
    await _handle_remote_command("allow-user telegram 123", state, console)
    assert bot._config.allowed_user_ids == {"123"}
    save.assert_awaited_once_with({"telegram": {"allowed_user_ids": ["123"]}})
    saved["telegram"]["allowed_user_ids"] = ["123"]
    await _handle_remote_command("deny-user telegram 123", state, console)
    assert not bot._config.allowed_user_ids
    assert save.call_args.args[0] == {"telegram": {"allowed_user_ids": []}}
    await _handle_remote_command("allow-user telegram everybody", state, console)
    assert save.await_count == 2


@pytest.mark.asyncio
async def test_authorized_group_prompt_routes_to_topic_but_other_senders_cannot():
    bot = bridge()
    bot._config.allowed_user_ids = {"123"}
    bot._session_topics = {"root": ("session-alpha", 55)}
    bot._api_call = AsyncMock(return_value={"result": {"username": "nova"}})
    bot._get_updates = AsyncMock(
        side_effect=[
            [
                {
                    "update_id": mid,
                    "message": {
                        "chat": {"id": 7, "type": "supergroup"},
                        "from": {"id": user, "is_bot": is_bot},
                        "message_thread_id": 55,
                        "is_topic_message": True,
                        "text": "fix the bug",
                    },
                }
                for mid, user, is_bot in [(1, 999, False), (2, 123, True), (3, 123, False)]
            ],
            asyncio.CancelledError(),
        ]
    )
    await bot.run()
    assert bot._queue.qsize() == 1
    assert bot._queue.get_nowait().thread_id == 55
