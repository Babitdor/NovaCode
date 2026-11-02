"""Bridge approval prompts report delivery failures and recheck live sender permissions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.remote.bridge import BridgeConfig, RemoteMessage, RemotePlatform
from novacode_cli.remote.discord_bridge import DiscordBridge
from novacode_cli.remote.telegram_bridge import TelegramBridge


@pytest.fixture(autouse=True)
def isolated_remote_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novacode_cli.remote.config.load_remote_config", dict)
    monkeypatch.setattr("novacode_cli.remote.discord_bridge._debug_log", AsyncMock())


async def telegram_target(source: str, *, private: bool) -> tuple[TelegramBridge, RemoteMessage]:
    chat_id = 42 if private else -100
    bot = TelegramBridge(
        BridgeConfig(RemotePlatform.TELEGRAM, "test-token", chat_id, allowed_user_ids={"42"}),
        asyncio.Queue(),
    )
    bot._session_topics = {"root": ("session", 55)}
    if source == "incoming":
        bot._api_call = AsyncMock(return_value={"result": {"username": "nova"}})
        bot._get_updates = AsyncMock(
            side_effect=[
                [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": chat_id, "type": "private" if private else "supergroup"},
                            "from": {"id": 43 if private else 42},
                            "text": "request",
                            "message_thread_id": 55,
                            "is_topic_message": True,
                        },
                    },
                ],
                asyncio.CancelledError(),
            ]
        )
        await bot.run()
        result = bot._queue.get_nowait()
    bot._running, bot.bot_user = True, "nova"
    if source == "stream":
        result = await bot.stream_target()
    assert result is not None
    return bot, result


async def discord_target(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    private: bool,
) -> tuple[DiscordBridge, RemoteMessage, SimpleNamespace]:
    import discord

    channel = SimpleNamespace(
        id=999,
        guild=None if private else object(),
        recipient=SimpleNamespace(id=42),
        typing=AsyncMock(),
        send=AsyncMock(),
    )
    bot = DiscordBridge(
        BridgeConfig(RemotePlatform.DISCORD, "test-token", "999", allowed_user_ids={"42"}),
        asyncio.Queue(),
    )
    if source == "incoming":
        events = {}
        client = AsyncMock()
        client.user = object()
        client.event = lambda event: events.setdefault(event.__name__, event)
        client.start = AsyncMock(side_effect=asyncio.CancelledError())
        client.is_closed = Mock(return_value=True)
        monkeypatch.setattr(discord, "Client", lambda **_kwargs: client)
        await bot.run()
        bot._connected = True
        incoming = SimpleNamespace(
            id=1,
            content="request",
            author=SimpleNamespace(
                id=43 if private else 42,
                bot=False,
                mention="@peer",
            ),
            guild=channel.guild,
            channel=channel,
            add_reaction=AsyncMock(),
        )
        await events["on_message"](incoming)
        result = bot._queue.get_nowait()
    else:
        bot._connected = True
        bot._client = SimpleNamespace(get_channel=lambda _channel_id: channel)
        result = await bot.stream_target()
    assert result is not None
    return bot, result, channel


async def test_telegram_shared_sender_callback_rechecks_permissions_and_connection() -> None:
    bot, target = await telegram_target("incoming", private=False)
    assert target.approval_check is not None
    assert target.approval_check("42")
    assert not target.approval_check("43")
    bot._config.allowed_user_ids = {"43"}
    assert not target.approval_check("42")
    assert target.approval_check("43")
    bot._running = False
    assert not target.approval_check("43")


async def test_telegram_dm_callback_uses_private_chat_peer_not_incoming_author() -> None:
    bot, target = await telegram_target("incoming", private=True)
    bot._config.allowed_user_ids = {"43"}
    assert target.approval_check("42")
    assert not target.approval_check("43")
    bot._running = False
    assert not target.approval_check("42")


async def test_discord_shared_sender_callback_rechecks_permissions_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, target, _channel = await discord_target(monkeypatch, "incoming", private=False)
    assert target.approval_check("42")
    assert not target.approval_check("43")
    bot._config.allowed_user_ids = {"43"}
    assert not target.approval_check("42")
    assert target.approval_check("43")
    bot._connected = False
    assert not target.approval_check("43")


async def test_discord_dm_callback_uses_channel_recipient_not_channel_id_or_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, target, channel = await discord_target(monkeypatch, "incoming", private=True)
    bot._config.allowed_user_ids = {"43", "999"}
    assert target.approval_check("42")
    assert not target.approval_check("43")
    assert not target.approval_check("999")
    channel.recipient = None
    assert not target.approval_check("42")


async def test_telegram_approval_callback_rechecks_chat_and_topic_authorization() -> None:
    bot, target = await telegram_target("incoming", private=False)
    assert target.approval_check("42")
    bot._config.chat_id = -200
    assert not target.approval_check("42")
    bot._config.allowed_ids.add(-100)
    assert target.approval_check("42")
    bot._config.allowed_ids.clear()
    assert not target.approval_check("42")
    bot._config.chat_id = -100
    bot._session_topics.clear()
    assert not target.approval_check("42")
    bot._topic_handlers[55] = object()
    assert target.approval_check("42")
    bot._topic_handlers.clear()
    assert not target.approval_check("42")


async def test_discord_approval_callback_rechecks_channel_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, target, _channel = await discord_target(monkeypatch, "incoming", private=False)
    assert target.approval_check("42")
    bot._config.chat_id = "1000"
    assert not target.approval_check("42")
    bot._config.allowed_ids.add("999")
    assert target.approval_check("42")
    bot._config.allowed_ids.clear()
    assert not target.approval_check("42")


async def test_discord_approval_callback_rechecks_registered_thread_lifetime() -> None:
    bot = DiscordBridge(
        BridgeConfig(RemotePlatform.DISCORD, "test-token", "999", allowed_user_ids={"42"}),
        asyncio.Queue(),
    )
    bot._connected = True
    thread = SimpleNamespace(id=1001, parent_id=999)
    bot._thread_channels[1001] = thread
    check = bot._approval_check(thread, private=False)
    assert check("42")
    del bot._thread_channels[1001]
    assert not check("42")
    bot._thread_channels[1001] = thread
    assert check("42")
    bot._config.chat_id = "2000"
    assert not check("42")


async def test_telegram_ask_delivers_to_topic_and_retracts_exact_messages() -> None:
    bot, target = await telegram_target("incoming", private=False)
    bot._send_message = AsyncMock(return_value=[201, 202])
    bot._delete_messages = AsyncMock()
    target.route["sid"] = "root"
    assert target.thread_id == 55
    retract = await target.ask_fn("approve scoped action")
    bot._send_message.assert_awaited_once_with(
        -100,
        "approve scoped action",
        thread_id=55,
        sid="root",
        require_complete=True,
    )
    await retract()
    bot._delete_messages.assert_awaited_once_with(-100, [201, 202])


async def test_telegram_ask_reports_zero_delivery_and_disconnect() -> None:
    bot, target = await telegram_target("incoming", private=False)
    bot._send_message = AsyncMock(return_value=[])
    with pytest.raises(RuntimeError, match="deliver"):
        await target.ask_fn("approval prompt")
    bot._running = False
    with pytest.raises(RuntimeError, match="disconnected"):
        await target.ask_fn("approval prompt")
    assert bot._send_message.await_count == 1


async def test_discord_ask_sends_chunks_and_retracts_only_its_prompt_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.remote.discord_format.render", lambda _text: ["first", "last"]
    )
    bot, target, channel = await discord_target(monkeypatch, "incoming", private=False)
    pages = [SimpleNamespace(id=i, delete=AsyncMock()) for i in (201, 202)]
    channel.send.side_effect = pages
    retract = await target.ask_fn("approval prompt")
    assert [call.args[0] for call in channel.send.await_args_list] == ["first", "last"]
    assert all(
        not call.kwargs["allowed_mentions"].everyone for call in channel.send.await_args_list
    )
    assert set(bot._outgoing_owners) == {201, 202}
    await retract()
    for page in pages:
        page.delete.assert_awaited_once()
    assert not bot._outgoing_owners


async def test_discord_partial_delivery_failure_propagates_and_cleans_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.remote.discord_format.render", lambda _text: ["first", "last"]
    )
    bot, target, channel = await discord_target(monkeypatch, "incoming", private=False)
    first = SimpleNamespace(id=201, delete=AsyncMock())
    channel.send.side_effect = [first, RuntimeError("transport unavailable")]
    with pytest.raises(RuntimeError, match="transport unavailable"):
        await target.ask_fn("approval prompt")
    first.delete.assert_awaited_once()
    assert not bot._outgoing_owners


async def test_discord_ask_rejects_disconnected_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    bot, target, channel = await discord_target(monkeypatch, "incoming", private=False)
    bot._connected = False
    with pytest.raises(RuntimeError, match="disconnected"):
        await target.ask_fn("approval prompt")
    channel.send.assert_not_awaited()


async def test_discord_cancelled_send_removes_already_delivered_prompt_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.remote.discord_format.render", lambda _text: ["first", "last"]
    )
    bot, target, channel = await discord_target(monkeypatch, "incoming", private=False)
    first = SimpleNamespace(id=201, delete=AsyncMock())
    channel.send.side_effect = [first, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await target.ask_fn("approval prompt")
    first.delete.assert_awaited_once()
    assert not bot._outgoing_owners


@pytest.mark.parametrize("failure", ["forbidden", "plain_fallback", "cancelled"])
async def test_telegram_complete_prompt_failure_removes_delivered_chunks(failure: str) -> None:
    bot, target = await telegram_target("incoming", private=False)
    target.route["sid"] = "root"
    sends: list[dict] = []
    deleted: list[int] = []

    async def api_call(method: str, payload: dict) -> dict | None:
        if method == "deleteMessage":
            deleted.append(payload["message_id"])
            return {"ok": True}
        assert method == "sendMessage"
        sends.append(payload)
        if len(sends) == 1:
            return {"ok": True, "result": {"message_id": 201}}
        if failure == "cancelled":
            raise asyncio.CancelledError
        bot._last_api_error = "can't parse entities" if failure == "plain_fallback" else "Forbidden"
        return None

    bot._api_call = AsyncMock(side_effect=api_call)
    prompt = "**Argument details**: example\n" * 500
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await target.ask_fn(prompt)
    else:
        with pytest.raises(RuntimeError, match="complete approval prompt"):
            await target.ask_fn(prompt)
    assert deleted == [201]
    assert not bot._owner
    assert len(sends) == (3 if failure == "plain_fallback" else 2)
    assert all(payload["message_thread_id"] == 55 for payload in sends)
    if failure == "plain_fallback":
        assert sends[1]["parse_mode"] == "HTML"
        assert "parse_mode" not in sends[2]


async def test_telegram_complete_prompt_success_retracts_every_rendered_chunk() -> None:
    bot, target = await telegram_target("incoming", private=False)
    target.route["sid"] = "root"
    delivered: list[int] = []
    deleted: list[int] = []

    async def api_call(method: str, payload: dict) -> dict:
        if method == "deleteMessage":
            deleted.append(payload["message_id"])
            return {"ok": True}
        delivered.append(201 + len(delivered))
        return {"ok": True, "result": {"message_id": delivered[-1]}}

    bot._api_call = AsyncMock(side_effect=api_call)
    retract = await target.ask_fn("**Argument details**: example\n" * 500)
    assert len(delivered) > 1
    assert set(bot._owner) == set(delivered)
    await retract()
    assert deleted == delivered
    assert not bot._owner


async def test_telegram_ordinary_messages_keep_best_effort_partial_delivery() -> None:
    bot, _target = await telegram_target("incoming", private=False)
    sends = 0

    async def api_call(method: str, _payload: dict) -> dict | None:
        nonlocal sends
        assert method == "sendMessage"
        sends += 1
        if sends == 1:
            return {"ok": True, "result": {"message_id": 201}}
        bot._last_api_error = "Forbidden"
        return None

    bot._api_call = AsyncMock(side_effect=api_call)
    assert await bot._send_message(-100, "**Argument details**: example\n" * 500) == [201]
    assert sends > 1


async def test_terminal_stream_mirrors_do_not_gain_approval_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _telegram, telegram = await telegram_target("stream", private=False)
    _discord, discord, _channel = await discord_target(monkeypatch, "stream", private=False)
    for target in (telegram, discord):
        assert target.ask_fn is None
        assert target.approval_check is None
