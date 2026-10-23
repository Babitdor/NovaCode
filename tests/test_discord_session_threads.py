"""Discord session threads retain history, isolation and existing remote routing."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from langgraph.store.memory import InMemoryStore
from rich.console import Console

from novacode_cli.remote.bridge import BridgeConfig, RemoteBridgeManager, RemotePlatform
from novacode_cli.remote.discord_bridge import DiscordBridge
from novacode_cli.remote.routing import RemoteRouter


def api_error(kind, status):
    return kind(
        SimpleNamespace(status=status, reason="test", headers={}), {"message": "test", "code": 0}
    )


@pytest.fixture
def kit(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    monkeypatch.setattr("novacode_cli.remote.config.load_remote_config", lambda: {})
    channels = {}
    parent = Mock(spec=discord.TextChannel)
    parent.id = 123
    channels[123] = parent
    counter = 1000

    def thread(tid, name, *, parent_id=123):
        obj = Mock(spec=discord.Thread)
        obj.id, obj.parent_id, obj.name = tid, parent_id, name
        obj.archived, obj.locked = False, False
        obj.join = AsyncMock()
        obj.typing = AsyncMock()
        obj.send = AsyncMock(
            return_value=SimpleNamespace(id=tid + 100, edit=AsyncMock(), delete=AsyncMock())
        )

        async def edit(**changes):
            for key, value in changes.items():
                if key != "reason":
                    setattr(obj, key, value)
            return obj

        obj.edit = AsyncMock(side_effect=edit)
        channels[tid] = obj
        return obj

    async def create(**options):
        nonlocal counter
        obj = thread(counter, options["name"])
        counter += 1
        return obj

    parent.create_thread = AsyncMock(side_effect=create)
    parent.send = AsyncMock()

    async def fetch(cid):
        if cid not in channels:
            raise api_error(discord.NotFound, 404)
        return channels[cid]

    client = SimpleNamespace(get_channel=channels.get, fetch_channel=AsyncMock(side_effect=fetch))

    def bridge(token="test-token"):
        config = BridgeConfig(RemotePlatform.DISCORD, token, "123", allowed_user_ids={"42"})
        bot = DiscordBridge(config, asyncio.Queue())
        bot._client, bot._connected = client, True
        return bot

    return SimpleNamespace(
        store=store, parent=parent, thread=thread, channels=channels, client=client, bridge=bridge
    )


async def test_concurrent_sync_creates_one_thread_and_avoids_repeated_api_calls(kit):
    bot = kit.bridge()
    results = await asyncio.gather(
        *(bot.ensure_session_topic("root", "session-alpha", "Main") for _ in range(5))
    )
    assert results == [1000] * 5
    kit.parent.create_thread.assert_awaited_once()
    options = kit.parent.create_thread.await_args.kwargs
    assert options["type"] == discord.ChannelType.public_thread
    assert len(options["name"]) <= 100
    assert "Main" in options["name"]
    kit.channels[1000].join.assert_awaited_once()
    assert not kit.channels[1000].edit.await_count


async def test_reconnect_resume_rename_and_archive_keep_the_same_history(kit):
    first = kit.bridge()
    tid = await first.ensure_session_topic("root", "session-alpha", "Old title")
    await first.close_topic(tid)
    assert kit.channels[tid].archived
    assert not first._session_threads
    resumed = kit.bridge()
    assert await resumed.ensure_session_topic("root", "session-alpha", "New title") == tid
    assert not kit.channels[tid].archived
    assert "New title" in kit.channels[tid].name
    kit.parent.create_thread.assert_awaited_once()
    assert resumed._session_threads == {"root": ("session-alpha", tid)}


async def test_switching_durable_sessions_archives_previous_root_thread(kit):
    bot = kit.bridge()
    previous = await bot.ensure_session_topic("root", "session-alpha", "Main")
    current = await bot.ensure_session_topic("root", "session-beta", "Main")
    assert previous != current
    assert kit.channels[previous].archived
    assert not kit.channels[current].archived
    assert bot._session_threads["root"] == ("session-beta", current)


async def test_deleted_thread_is_replaced_but_forbidden_is_not(kit):
    bot = kit.bridge()
    tid = await bot.ensure_session_topic("root", "session-alpha", "Main")
    del kit.channels[tid]
    restarted = kit.bridge()
    assert await restarted.ensure_session_topic("root", "session-alpha", "Main") == 1001
    denied = kit.bridge()
    kit.client.fetch_channel.side_effect = api_error(discord.Forbidden, 403)
    with pytest.raises(discord.Forbidden):
        await denied.ensure_session_topic("root", "session-alpha", "Main")
    assert kit.parent.create_thread.await_count == 2


async def test_foreign_saved_thread_never_receives_output(kit):
    bot = kit.bridge()
    kit.thread(999, "Unrelated", parent_id=456)
    await kit.store.aput(bot._thread_namespace(), "session-alpha", {"thread": 999})
    with pytest.raises(RuntimeError, match="outside"):
        await bot.ensure_session_topic("root", "session-alpha", "Main")
    kit.parent.create_thread.assert_not_awaited()
    assert not bot._session_threads


async def test_bot_scoped_persistence_and_different_sessions_never_share_threads(kit):
    bot = kit.bridge()
    a = await bot.ensure_session_topic("root", "session-alpha", "Main")
    b = await bot.ensure_session_topic("child", "session-beta", "Worker")
    other_bot = kit.bridge(token="another-bot")
    c = await other_bot.ensure_session_topic("root", "session-alpha", "Main")
    assert len({a, b, c}) == 3


async def test_forum_channel_creates_a_post_instead_of_sending_to_parent(kit):
    forum = Mock(spec=discord.ForumChannel)
    forum.id = 123
    thread = kit.thread(1000, "Main")
    forum.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread))
    kit.channels[123] = forum
    bot = kit.bridge()
    assert await bot.ensure_session_topic("root", "session-alpha", "Main") == 1000
    options = forum.create_thread.await_args.kwargs
    assert "content" in options and "type" not in options
    assert options["allowed_mentions"].everyone is False


async def test_direct_messages_keep_existing_destination(kit):
    dm = Mock(spec=discord.DMChannel)
    dm.id, dm.send = 123, AsyncMock()
    kit.channels[123] = dm
    bot = kit.bridge()
    assert await bot.ensure_session_topic("root", "session-alpha", "Main") is None
    target = await bot.stream_target()
    assert target.thread_id is None
    await target.reply_fn("Still a direct message")
    dm.send.assert_awaited_once()
    kit.parent.create_thread.assert_not_awaited()


async def test_watchdog_retains_session_bindings_without_reusing_old_clients(kit, monkeypatch):
    previous = kit.bridge()
    await previous.ensure_session_topic("root", "session-alpha", "Main")
    previous._outgoing_owners[777] = "root"
    dead = asyncio.create_task(asyncio.sleep(0))
    await dead
    manager = RemoteBridgeManager(asyncio.Queue())
    manager._WATCHDOG_INTERVAL = 0.001
    manager._bridges["discord:123"] = {"bridge": previous, "config": previous._config, "task": dead}
    restarted = asyncio.Event()

    async def run(self):
        restarted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(DiscordBridge, "run", run)
    manager._watchdog_task = asyncio.create_task(manager._watchdog())
    try:
        await asyncio.wait_for(restarted.wait(), 3)
        replacement = manager._bridges["discord:123"]["bridge"]
        assert replacement._session_threads == previous._session_threads
        assert replacement._session_thread_names == previous._session_thread_names
        assert replacement._outgoing_owners[777] == "root"
        assert not replacement._thread_channels
        assert replacement._client is None
    finally:
        await manager.stop_all()
        await manager._watchdog_task


async def test_failed_save_retries_without_creating_another_thread(kit, monkeypatch):
    bot = kit.bridge()
    save = AsyncMock(side_effect=[RuntimeError("storage unavailable"), None])
    monkeypatch.setattr(
        "novacode_cli.memory.store.get_durable_store",
        lambda: SimpleNamespace(aget=kit.store.aget, aput=save),
    )
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await bot.ensure_session_topic("root", "session-alpha", "Main")
    assert await bot.ensure_session_topic("root", "session-alpha", "Main") == 1000
    kit.parent.create_thread.assert_awaited_once()
    assert not bot._unsaved_threads


async def test_local_streams_go_to_their_own_thread_and_restore_archived_threads(kit):
    bot = kit.bridge()
    await bot.ensure_session_topic("root", "session-alpha", "Main")
    await bot.ensure_session_topic("child", "session-beta", "Worker")
    kit.channels[1001].archived = True
    target = await bot.stream_target(sid="child")
    assert target.chat_id == "123" and target.thread_id == 1001
    assert target.route == {"sid": "child", "thread": 1001}
    await target.reply_fn("Worker response")
    kit.channels[1001].send.assert_awaited_once()
    kit.channels[1000].send.assert_not_awaited()
    assert not kit.channels[1001].archived
    assert bot._outgoing_owners[1101] == "child"
    assert await bot.stream_target(sid="missing-child") is None


async def test_permission_failure_can_keep_legacy_parent_chat_without_mixing_children(kit):
    bot = kit.bridge()
    kit.parent.create_thread.side_effect = api_error(discord.Forbidden, 403)
    with pytest.raises(discord.Forbidden):
        await bot.ensure_session_topic("root", "session-alpha", "Main")
    root = await bot.stream_target()
    assert root.thread_id is None
    await root.reply_fn("Parent channel still works")
    kit.parent.send.assert_awaited_once()
    assert await bot.stream_target(sid="child") is None


async def test_status_lists_threads_and_test_message_uses_main_thread(kit):
    from novacode_cli.commands.commands import _handle_remote_command

    bot = kit.bridge()
    await bot.ensure_session_topic("root", "session-alpha", "Main")
    kit.parent.send.reset_mock()
    manager = RemoteBridgeManager(bot._queue)
    manager._bridges["discord:123"] = {
        "config": bot._config,
        "bridge": bot,
        "task": SimpleNamespace(done=lambda: False),
    }
    state = SimpleNamespace(_remote_bridge_manager=manager)
    output = io.StringIO()
    console = Console(file=output, color_system=None)
    await _handle_remote_command("status", state, console)
    assert "Thread 1000" in output.getvalue()
    await _handle_remote_command("test", state, console)
    kit.channels[1000].send.assert_awaited_once()
    kit.parent.send.assert_not_awaited()


async def test_thread_routing_survives_bridge_recreation_before_ui_sync(kit):
    router = RemoteRouter()
    message = SimpleNamespace(
        platform=RemotePlatform.DISCORD,
        chat_id="123",
        thread_id=1001,
        text="Continue",
        route={"thread_session": "child"},
    )
    assert router.resolve(message, {"root": "Main", "child": "Worker"}).sid == "child"
    assert router.resolve(message, {"root": "Main"}) is None
    message.platform = RemotePlatform.TELEGRAM
    assert router.resolve(message, {"root": "Main", "child": "Worker"}) is None


def test_closed_thread_cannot_reuse_a_stale_ui_route():
    router = RemoteRouter(topics={("123", "root"): 1000})
    message = SimpleNamespace(
        platform=RemotePlatform.DISCORD,
        chat_id="123",
        thread_id=1000,
        text="Continue",
        route={"thread_session": None},
    )
    assert router.resolve(message, {"root": "A different session"}) is None


async def test_tui_sync_and_close_cover_both_platforms(kit, monkeypatch):
    from novacode_cli.tui.app import NovaApp

    bot = kit.bridge()
    manager = RemoteBridgeManager(asyncio.Queue())
    manager.bridges = lambda platform=None: [bot] if platform == RemotePlatform.DISCORD else []
    app = NovaApp.__new__(NovaApp)
    app.session_state = SimpleNamespace(session_id="session-alpha", _remote_bridge_manager=manager)
    root = SimpleNamespace(sid="root", title="Main", kind="root", state={})
    child = SimpleNamespace(
        sid="child",
        title="Worker",
        kind="child",
        status="idle",
        state={"session_state": SimpleNamespace(session_id="session-beta")},
    )
    app._root_pane = app._active_pane = root
    app._panes = [root, child]
    app._remote_router = RemoteRouter()
    monkeypatch.setattr(app, "_remote_telegram_bridges", lambda: [])
    await app._sync_remote_topics()
    await app._sync_remote_topics()
    assert app._remote_router.topics == {("123", "root"): 1000, ("123", "child"): 1001}
    assert kit.parent.create_thread.await_count == 2
    assert NovaApp._tab_remote_platforms(app, child) == ["DC"]
    await app._close_remote_topics(child)
    assert kit.channels[1001].archived
    assert ("123", "child") not in app._remote_router.topics
    assert not kit.channels[1000].archived


async def test_late_gateway_connection_syncs_sessions_without_another_prompt():
    from novacode_cli.tui.app import NovaApp

    queue = asyncio.Queue()
    manager = RemoteBridgeManager(queue)
    state = SimpleNamespace(_remote_message_queue=queue, _remote_bridge_manager=manager)
    sync = AsyncMock()
    tasks = []

    def run_worker(coroutine, **kwargs):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    app = SimpleNamespace(
        _remote_owner_state=lambda: state,
        _remote_consumer_worker=SimpleNamespace(is_finished=False),
        _remote_topics_worker=SimpleNamespace(is_finished=False),
        _sync_remote_topics=sync,
        _log=lambda text: None,
        run_worker=run_worker,
        is_running=True,
    )
    NovaApp._ensure_remote_consumer(app)
    await manager._on_status("Discord bridge connected as Nova (1 server(s))")
    await asyncio.gather(*tasks)
    sync.assert_awaited_once()


async def test_gateway_filters_unapproved_senders_and_unregistered_threads(kit, monkeypatch):
    bot = kit.bridge()
    await bot.ensure_session_topic("child", "session-beta", "Worker")
    events = {}
    ready = asyncio.Event()

    class Client:
        user = SimpleNamespace(id=999, bot=True)
        guilds = []

        def __init__(self, **kwargs):
            pass

        def get_channel(self, cid):
            return kit.channels.get(cid)

        async def fetch_channel(self, cid):
            return await kit.client.fetch_channel(cid)

        def event(self, callback):
            events[callback.__name__] = callback
            return callback

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def is_closed(self):
            return False

        async def close(self):
            pass

        async def start(self, token):
            await events["on_ready"]()
            ready.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(discord, "Client", Client)
    monkeypatch.setattr("novacode_cli.remote.discord_bridge._debug_log", AsyncMock())
    task = asyncio.create_task(bot.run())
    try:
        await asyncio.wait_for(ready.wait(), 3)
        thread = kit.channels[1000]

        def message(channel=thread, user=42):
            return SimpleNamespace(
                id=1234,
                channel=channel,
                author=SimpleNamespace(id=user, bot=False, mention=f"<@{user}>"),
                guild=SimpleNamespace(id=7),
                content="Continue the work",
                reference=None,
                add_reaction=AsyncMock(),
            )

        await events["on_message"](message(user=43))
        await events["on_message"](message(kit.thread(888, "Unregistered")))
        assert bot._queue.empty()
        await events["on_message"](message())
        incoming = bot._queue.get_nowait()
        assert incoming.chat_id == "123" and incoming.thread_id == 1000
        assert incoming.sender_id == 42
        assert RemoteRouter().resolve(incoming, {"root": "Main", "child": "Worker"}).sid == "child"
        incoming.route["sid"] = "child"
        await incoming.reply_fn("Reply inside the thread")
        thread.send.assert_awaited_once()
        assert bot._outgoing_owners[1100] == "child"
        del kit.channels[1000]
        await events["on_raw_thread_delete"](SimpleNamespace(thread_id=1000))
        assert await bot.ensure_session_topic("child", "session-beta", "Worker") == 1001
        await events["on_message"](message(channel=kit.channels[1001]))
        replacement = bot._queue.get_nowait()
        assert replacement.route["thread_session"] == "child"
    finally:
        task.cancel()
        await task
