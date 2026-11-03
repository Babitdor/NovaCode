"""Native Telegram approval buttons retain the broker's authorization boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote import approvals
from novacode_cli.remote.approvals import RemoteApprovalBroker
from novacode_cli.remote.telegram_bridge import TelegramBridge
from tests.test_remote_approval_transport import (
    isolated_remote_config,  # noqa: F401
    telegram_target,
)
from tests.test_remote_approvals import payload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.mark.parametrize("choice", ["approve", "reject"])
@pytest.mark.parametrize("kind", ["tool", "plan"])
async def test_native_buttons_resolve_once_for_original_sender(choice: str, kind: str) -> None:
    bot, target = await telegram_target("incoming", private=False)
    target.route["sid"] = "root"
    sent = asyncio.Event()
    controls = {}
    next_id = 10

    async def api(method: str, params: dict, **_kwargs: object) -> dict:
        nonlocal next_id
        next_id += 1
        if method == "sendMessage" and "reply_markup" in params:
            controls.update(params)
            sent.set()
        return {"ok": True, "result": {"message_id": next_id}}

    bot._api_call = AsyncMock(side_effect=api)
    broker = RemoteApprovalBroker()
    data = payload() if kind == "tool" else {"plan": "Run the requested checks."}
    task = asyncio.create_task(broker.request(target, kind, data))
    await asyncio.wait_for(sent.wait(), 2)
    buttons = controls["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Approve", "Reject"]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)
    assert controls["message_thread_id"] == 55
    assert "Choose Approve or Reject below" in controls["text"]
    button = next(button for button in buttons if button["text"].lower() == choice)
    callback = {
        "id": "click",
        "data": button["callback_data"],
        "from": {"id": 43},
        "message": {
            "message_id": next_id,
            "chat": {"id": -100, "type": "supergroup"},
            "message_thread_id": 55,
        },
    }
    response = await bot._approval_callback(callback)
    assert response is not None
    assert await broker.handle(replace(target, text=response["text"], sender_id="43"))
    assert not task.done()
    assert await broker.handle(replace(target, text=response["text"], sender_id="42"))
    result = await asyncio.wait_for(task, 2)
    if kind == "tool":
        assert result["any_rejected"] is (choice == "reject")
    else:
        assert result["response"]["approved"] is (choice == "approve")
    assert not broker._pending
    bot._api_call.assert_any_await(
        "deleteMessage", {"chat_id": -100, "message_id": callback["message"]["message_id"]}
    )
    replay = replace(target, text=response["text"], reply_fn=AsyncMock())
    assert await broker.handle(replay)
    assert "No matching approval" in replay.reply_fn.call_args.args[0]


async def test_failed_prompt_delivery_retracts_details_and_rejects() -> None:
    bot, target = await telegram_target("incoming", private=False)
    bot._send_html = AsyncMock(side_effect=[{"result": {"message_id": 10}}, None])
    bot._delete_messages = AsyncMock()
    broker = RemoteApprovalBroker()
    data = {"action_requests": [{"name": "shell", "args": {"command": "x" * 5000}}]}
    result = await broker.request(target, "tool", data)
    assert result["any_rejected"]
    assert not broker._pending
    bot._delete_messages.assert_any_await(-100, [10])


@pytest.mark.parametrize("cancel", [True, False])
async def test_native_controls_retracted_on_cancel_or_expiry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel: bool,
) -> None:
    bot, target = await telegram_target("incoming", private=False)
    bot._api_call = AsyncMock(return_value={"result": {"message_id": 10}})
    sent = asyncio.Event()
    original = target.approval_fn

    async def show(text: str, nonce: str) -> Callable[[], Awaitable[None]] | None:
        retract = await original(text, nonce)
        sent.set()
        return retract

    target.approval_fn = show
    monkeypatch.setattr(approvals, "APPROVAL_TTL", 0.2)
    broker = RemoteApprovalBroker()
    task = asyncio.create_task(broker.request(target, "tool", payload()))
    await asyncio.wait_for(sent.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await asyncio.wait_for(task, 2))["any_rejected"]
    assert not broker._pending
    bot._api_call.assert_any_await("deleteMessage", {"chat_id": -100, "message_id": 10})


@pytest.mark.parametrize("data", ["other", "nova:approve:bad", None, 123])
async def test_invalid_callbacks_are_acknowledged_and_ignored(data: object) -> None:
    bot, _target = await telegram_target("incoming", private=False)
    bot._api_call = AsyncMock()
    assert await bot._approval_callback({"id": "click", "data": data, "message": {}}) is None
    bot._api_call.assert_awaited_once_with("answerCallbackQuery", {"callback_query_id": "click"})


async def test_polling_routes_real_click_sender_and_prompt_owner() -> None:
    bot, _target = await telegram_target("incoming", private=False)
    bot._owner[19] = "root"
    nonce = "a" * 32
    bot._api_call = AsyncMock(return_value={"result": {"username": "nova"}})
    bot._get_updates = AsyncMock(
        side_effect=[
            [
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "click",
                        "data": f"nova:approve:{nonce}",
                        "from": {"id": 42},
                        "message": {
                            "message_id": 19,
                            "chat": {"id": -100, "type": "supergroup"},
                            "from": {"id": 999, "is_bot": True},
                            "message_thread_id": 55,
                        },
                    },
                },
            ],
            asyncio.CancelledError(),
        ]
    )
    await bot.run()
    response = bot._queue.get_nowait()
    assert response.sender_id == "42"
    assert response.text == f"approve {nonce}"
    assert response.thread_id == 55
    assert response.reply_to_owner == "root"
    assert response.approval_fn is not None


async def test_poller_requests_callback_updates() -> None:
    bot, _target = await telegram_target("incoming", private=False)
    bot._api_call = AsyncMock(return_value={"result": []})
    await TelegramBridge._get_updates(bot)
    assert bot._api_call.call_args.args[1]["allowed_updates"] == ["message", "callback_query"]
