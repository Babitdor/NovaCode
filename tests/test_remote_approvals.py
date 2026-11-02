"""Remote approvals require explicit, scoped, single-use human responses."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote import approvals
from novacode_cli.remote.approvals import RemoteApprovalBroker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def message(text: str = "", **changes: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "platform": "telegram",
            "chat_id": 7,
            "thread_id": 42,
            "sender_id": "alice",
            "text": text,
            "reply_fn": AsyncMock(),
            **changes,
        }
    )


def payload() -> dict:
    return {
        "action_requests": [
            {"name": "shell", "args": {"command": "git status", "nested": [1, 2]}},
            {"name": "write_file", "args": {"file_path": "code.py", "content": "result"}},
        ]
    }


async def start(
    broker: RemoteApprovalBroker,
    msg: SimpleNamespace,
    kind: str = "tool",
    data: object = None,
    *,
    label: str = "",
) -> tuple[asyncio.Task, str]:
    sent = asyncio.Event()
    original = msg.reply_fn

    async def send(text: str) -> None:
        await original(text)
        if "\nApprove " in text:
            sent.set()

    msg.reply_fn = send
    task = asyncio.create_task(
        broker.request(msg, kind, payload() if data is None else data, label=label)
    )
    await asyncio.wait_for(sent.wait(), 2)
    nonce = re.search(r"\nApprove ([a-f0-9]+)", original.call_args.args[0])[1]
    return task, nonce


async def test_explicit_tool_approval_shows_all_actions_and_is_consumed_once() -> None:
    broker, msg = RemoteApprovalBroker(), message()
    sent = msg.reply_fn
    task, nonce = await start(broker, msg)
    body = sent.call_args.args[0]
    assert all(
        term in body
        for term in ("shell", "git status", "nested", "write_file", "code.py", "result")
    )
    assert await broker.handle(message(f"/APPROVE {nonce.upper()}"))
    assert await task == {
        "decisions": [{"type": "approve"}, {"type": "approve"}],
        "any_rejected": False,
    }
    assert not broker._pending
    replay = message(f"Approve {nonce}")
    assert await broker.handle(replay)
    assert "No matching" in replay.reply_fn.call_args.args[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "discord"),
        ("chat_id", 8),
        ("thread_id", 43),
        ("sender_id", "bob"),
        ("sender_id", None),
    ],
)
async def test_other_accounts_chats_topics_and_platforms_cannot_approve(
    field: str, value: object
) -> None:
    broker, msg = RemoteApprovalBroker(), message()
    task, nonce = await start(broker, msg)
    assert await broker.handle(message(f"Approve {nonce}", **{field: value}))
    assert not task.done()
    assert await broker.handle(message(f"Reject {nonce}"))
    assert await task == {"decisions": [], "any_rejected": True}


async def test_concurrent_requests_have_distinct_nonces_and_no_cross_approval() -> None:
    broker = RemoteApprovalBroker()
    first, token_a = await start(broker, message())
    second, token_b = await start(broker, message())
    assert token_a != token_b
    await broker.handle(message(f"Reject {token_b}"))
    assert (await second)["any_rejected"]
    assert not first.done()
    await broker.handle(message(f"Approve {token_a}"))
    assert not (await first)["any_rejected"]


async def test_plan_approval_is_manual_and_disables_plan_mode() -> None:
    broker = RemoteApprovalBroker()
    task, nonce = await start(
        broker, message(), "plan", {"plan": "# Plan\nPreserve all constraints"}
    )
    await broker.handle(message(f"Approve {nonce}"))
    assert await task == {
        "response": {"approved": True, "mode": "manual"},
        "state_update": {"plan_mode_enabled": False},
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"action_requests": []},
        {"action_requests": [{}]},
        {"action_requests": [{"name": "shell", "args": {1: "not JSON keys"}}]},
    ],
)
async def test_malformed_requests_fail_closed(data: object) -> None:
    broker = RemoteApprovalBroker()
    assert await broker.request(message(), "tool", data) == {"decisions": [], "any_rejected": True}
    assert not broker._pending


@pytest.mark.parametrize("sender", [None, "", " ", False])
async def test_missing_real_sender_cannot_create_remote_approval(sender: object) -> None:
    broker = RemoteApprovalBroker()
    assert (await broker.request(message(sender_id=sender), "tool", payload()))["any_rejected"]
    assert not broker._pending


async def test_unknown_and_malformed_commands_are_consumed_but_ordinary_text_is_not() -> None:
    broker = RemoteApprovalBroker()
    for text in ("hello", "approved yesterday", "/skills", "yes"):
        assert not await broker.handle(message(text))
    for text in ("Approve", "/Reject nonsense", "Approve nonce extra"):
        msg = message(text)
        assert await broker.handle(msg)
        assert (
            msg.reply_fn.call_args.args[0]
            == "No matching approval is available for this conversation."
        )


async def test_expired_nonce_rejects_and_cannot_be_used() -> None:
    broker = RemoteApprovalBroker()
    task, nonce = await start(broker, message())
    broker._pending[nonce].expires = 0
    await broker.handle(message(f"Approve {nonce}"))
    assert (await task)["any_rejected"]
    assert not broker._pending


async def test_timeout_rejects_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "APPROVAL_TTL", 0.01)
    broker = RemoteApprovalBroker()
    assert (await broker.request(message(), "tool", payload()))["any_rejected"]
    assert not broker._pending


async def test_cancel_retracts_prompt_and_cleans_up() -> None:
    broker, msg = RemoteApprovalBroker(), message()
    retract = AsyncMock()
    shown = asyncio.Event()

    async def ask(_text: str) -> AsyncMock:
        shown.set()
        return retract

    msg.ask_fn = ask
    task = asyncio.create_task(broker.request(msg, "tool", payload()))
    await shown.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not broker._pending
    retract.assert_awaited_once()


@pytest.mark.parametrize("fail", [False, True])
async def test_fast_reply_during_send_is_bound_and_send_failure_still_rejects(
    *, fail: bool
) -> None:
    broker = RemoteApprovalBroker()

    async def send(text: str) -> None:
        nonce = re.search(r"\nApprove ([a-f0-9]+)", text)[1]
        assert await broker.handle(message(f"Approve {nonce}"))
        if fail:
            error = "send failed"
            raise OSError(error)

    msg = message(reply_fn=send)
    result = await broker.request(msg, "tool", payload())
    assert result["any_rejected"] is fail
    assert not broker._pending


async def test_large_details_all_precede_nonce_and_payload_mutation_rejects() -> None:
    broker = RemoteApprovalBroker()
    data = {
        "action_requests": [
            {"name": "write_file", "args": {"content": "x" * 5000 + "```hidden tail"}}
        ]
    }
    prompts = []

    async def send(text: str) -> None:
        prompts.append(text)
        assert len(text) < 2000
        match = re.search(r"\nApprove ([a-f0-9]+)", text)
        if match:
            assert len(prompts) > 1
            assert "hidden tail" in text
            assert r"\u0060\u0060\u0060" in text
            data["action_requests"][0]["args"]["content"] = "changed after display"
            await broker.handle(message(f"Approve {match[1]}"))

    assert (await broker.request(message(reply_fn=send), "tool", data))["any_rejected"]
    assert all("\nApprove " not in part for part in prompts[:-1])


async def test_oversized_details_reject_without_hiding_actions() -> None:
    broker = RemoteApprovalBroker()
    data = {"action_requests": [{"name": "shell", "args": {"command": "x" * 100_001}}]}
    msg = message()
    assert (await broker.request(msg, "tool", data))["any_rejected"]
    assert "Approve " not in msg.reply_fn.call_args.args[0]


@pytest.mark.parametrize("platform", ["telegram", "discord"])
async def test_synthetic_origin_is_rejected_even_with_live_authorization_callback(
    platform: str,
) -> None:
    broker = RemoteApprovalBroker()
    authorized = {"alice"}
    msg = message(platform=platform, sender_id=None, approval_check=authorized.__contains__)
    assert (await broker.request(msg, "tool", payload()))["any_rejected"]
    assert not broker._pending
    assert "\nApprove " not in msg.reply_fn.call_args.args[0]


async def test_remote_origin_keeps_exact_sender_even_when_policy_allows_other_accounts() -> None:
    broker = RemoteApprovalBroker()
    msg = message(approval_check=lambda _sender: True)
    task, nonce = await start(broker, msg)
    await broker.handle(message(f"Approve {nonce}", sender_id="bob"))
    assert not task.done()
    await broker.handle(message(f"Reject {nonce}"))
    assert (await task)["any_rejected"]


async def test_authorization_revocation_after_response_still_rejects() -> None:
    broker = RemoteApprovalBroker()
    allowed = {"alice"}

    async def send(text: str) -> None:
        nonce = re.search(r"\nApprove ([a-f0-9]+)", text)[1]
        await broker.handle(message(f"Approve {nonce}"))
        allowed.clear()

    msg = message(approval_check=allowed.__contains__, reply_fn=send)
    assert (await broker.request(msg, "tool", payload()))["any_rejected"]


async def test_revoked_origin_policy_and_unsupported_platform_reject_without_prompt() -> None:
    broker = RemoteApprovalBroker()
    for msg in (
        message(approval_check=lambda _sender: False),
        message(platform="rootapp"),
        message(platform="webhook", sender_id=None, approval_check=lambda _sender: True),
    ):
        assert (await broker.request(msg, "tool", payload()))["any_rejected"]
        assert not broker._pending
    assert not await broker.handle(message("Approve unknown", platform="rootapp"))
    assert not await broker.handle(message("Reject unknown", platform="webhook"))


async def test_ask_retracts_on_success_and_rejection() -> None:
    for decision in ("Approve", "Reject"):
        broker, retract = RemoteApprovalBroker(), AsyncMock()

        async def ask(
            text: str,
            *,
            _broker: RemoteApprovalBroker = broker,
            _decision: str = decision,
            _retract: AsyncMock = retract,
        ) -> AsyncMock:
            nonce = re.search(r"\nApprove ([a-f0-9]+)", text)[1]
            await _broker.handle(message(f"{_decision} {nonce}"))
            return _retract

        result = await broker.request(message(ask_fn=ask), "tool", payload())
        assert result["any_rejected"] is (decision == "Reject")
        retract.assert_awaited_once()


async def test_failed_delivery_of_earlier_page_never_exposes_approval_command() -> None:
    broker, msg = RemoteApprovalBroker(), message()
    msg.reply_fn.side_effect = OSError("transport failed")
    data = {"action_requests": [{"name": "write_file", "args": {"content": "x" * 6000}}]}
    assert (await broker.request(msg, "tool", data))["any_rejected"]
    assert "\nApprove " not in msg.reply_fn.call_args.args[0]
    assert not broker._pending


async def test_cancel_label_rejects_only_that_sessions_pending_requests() -> None:
    broker = RemoteApprovalBroker()
    first, token_a = await start(broker, message(), label="Child (one)")
    second, token_b = await start(broker, message(), label="Child (two)")
    broker.cancel("Child (one)")
    assert (await first)["any_rejected"]
    assert not second.done()
    await broker.handle(message(f"Approve {token_a}"))
    await broker.handle(message(f"Approve {token_b}"))
    assert not (await second)["any_rejected"]


async def test_cancel_label_after_fast_reply_before_send_returns_still_rejects() -> None:
    broker = RemoteApprovalBroker()

    async def send(text: str) -> None:
        nonce = re.search(r"\nApprove ([a-f0-9]+)", text)[1]
        await broker.handle(message(f"Approve {nonce}"))
        broker.cancel("Child (one)")

    result = await broker.request(message(reply_fn=send), "tool", payload(), label="Child (one)")
    assert result["any_rejected"]
    assert not broker._pending


@pytest.mark.parametrize("kind", ["tool", "plan"])
@pytest.mark.parametrize(
    "change", ["unchanged", "revoke", "cancel", "mutate", "expire", "parent_cancel"]
)
async def test_approval_is_revalidated_after_blocked_prompt_retraction(
    kind: str, change: str
) -> None:
    broker = RemoteApprovalBroker()
    started, release = asyncio.Event(), asyncio.Event()
    allowed = {"alice"}
    data = payload() if kind == "tool" else {"plan": "Original reviewed plan"}

    async def retract() -> None:
        started.set()
        await release.wait()

    async def ask(text: str) -> Callable[[], Awaitable[None]]:
        nonce = re.search(r"\nApprove ([a-f0-9]+)", text)[1]
        await broker.handle(message(f"Approve {nonce}"))
        return retract

    msg = message(ask_fn=ask, approval_check=allowed.__contains__)
    task = asyncio.create_task(broker.request(msg, kind, data, label="Session one"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done()
        assert len(broker._pending) == 1
        if change == "revoke":
            allowed.clear()
        elif change == "cancel":
            broker.cancel("Session one")
        elif change == "mutate":
            if kind == "tool":
                data["action_requests"][0]["args"]["command"] = "unreviewed action"
            else:
                data["plan"] = "Unreviewed replacement plan"
        elif change == "expire":
            next(iter(broker._pending.values())).expires = 0
        elif change == "parent_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not broker._pending
            return
        release.set()
        result = await asyncio.wait_for(task, 2)
        approved = not result["any_rejected"] if kind == "tool" else result["response"]["approved"]
        assert approved is (change == "unchanged")
        assert not broker._pending
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
