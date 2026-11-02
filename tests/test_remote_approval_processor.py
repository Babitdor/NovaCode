"""Compatibility renderer approvals remain interactive while the queue is busy."""

from __future__ import annotations

import asyncio
import io
import re
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from novacode_cli import ui_events as ev
from novacode_cli.remote import approvals, processor
from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.ui import execution

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def message(text: str, sender: str = "owner") -> SimpleNamespace:
    sent = asyncio.Event()
    replies = []

    async def reply(text: str) -> None:
        replies.append(text)
        sent.set()

    return SimpleNamespace(
        platform=RemotePlatform.TELEGRAM,
        chat_id=7,
        thread_id=None,
        sender_id=sender,
        user_name=sender,
        text=text,
        images=[],
        reply_fn=reply,
        replies=replies,
        sent=sent,
    )


@pytest.fixture
async def running_processor(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    from novacode_cli.vixie import server

    for name in ("set_idle", "set_thinking", "set_working"):
        monkeypatch.setattr(server, name, AsyncMock())
    console = Console(file=io.StringIO())
    monkeypatch.setattr(execution, "console", console)
    monkeypatch.setattr(processor, "_debug_log", AsyncMock())
    broker = approvals.RemoteApprovalBroker()
    monkeypatch.setattr(approvals, "RemoteApprovalBroker", lambda: broker)
    previous_handler = AsyncMock()
    session = SimpleNamespace(
        thread_id="session",
        session_id="session",
        auto_approve=False,
        plan_mode_enabled=False,
        steering_instructions=[],
        _remote_approval_handler=previous_handler,
    )
    agent = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={})))
    runtime = SimpleNamespace(
        queue=asyncio.Queue(),
        session=session,
        previous_handler=previous_handler,
        broker=broker,
        kind="tool",
        payload={"action_requests": [{"name": "shell", "args": {"command": "pytest -q"}}]},
        responses=[],
    )

    async def source(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        future = asyncio.get_running_loop().create_future()
        runtime.future = future
        try:
            yield ev.InterruptRequest(kind=runtime.kind, payload=runtime.payload, future=future)
        finally:
            if future.done():
                runtime.responses.append(future.result())
        yield ev.Done()

    monkeypatch.setattr(execution, "run_with_goal", source)
    task = asyncio.create_task(
        processor.remote_message_processor(
            runtime.queue,
            agent,
            "nova-agent",
            session,
            console,
            None,
            None,
            None,
            set(),
            execute_fn=execution.execute_task,
        )
    )
    try:
        yield runtime
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def nonce(origin: SimpleNamespace) -> str:
    match = re.search(r"Approve ([0-9a-f]{32})", "\n".join(origin.replies))
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("kind", ["tool", "plan"])
@pytest.mark.parametrize("decision", ["Approve", "Reject"])
async def test_paused_turn_accepts_explicit_approval_or_rejection(
    running_processor: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    decision: str,
) -> None:
    runtime = running_processor
    runtime.kind = kind
    resolved_on = []
    caller = threading.get_ident()

    def resolve(*_args: object, **_kwargs: object) -> tuple[str, None]:
        resolved_on.append(threading.get_ident())
        return "Saved plan: update the parser, then run tests.", None

    if kind == "plan":
        runtime.payload = {}
        monkeypatch.setattr("novacode_cli.ui.interrupt_handlers.resolve_plan_content", resolve)
    origin = message("perform task")
    await runtime.queue.put(origin)
    await asyncio.wait_for(origin.sent.wait(), 3)
    assert runtime.responses == [], "a genuine ask must wait even though the turn lock is held"
    assert runtime.session._remote_message_lock.locked()
    reply = message(f"{decision} {nonce(origin)}")
    await runtime.queue.put(reply)
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert len(runtime.responses) == 1
    response = runtime.responses[0]
    approved = decision == "Approve"
    if kind == "tool":
        assert response["any_rejected"] is not approved
        assert response["decisions"] == ([{"type": "approve"}] if approved else [])
    else:
        assert response["response"]["approved"] is approved
        assert response["state_update"] == ({"plan_mode_enabled": False} if approved else {})
        assert resolved_on
        assert resolved_on[0] != caller
        assert "Saved plan" in "\n".join(origin.replies)
    assert not runtime.broker._pending
    assert runtime.session._remote_approval_handler is runtime.previous_handler
    assert not runtime.session._remote_message_lock.locked()
    assert not runtime.session.auto_approve


async def test_another_sender_cannot_approve_or_steer_a_paused_request(
    running_processor: SimpleNamespace,
) -> None:
    runtime = running_processor
    origin = message("perform task")
    await runtime.queue.put(origin)
    await asyncio.wait_for(origin.sent.wait(), 3)
    token = nonce(origin)
    stranger = message(f"Approve {token}", sender="another-user")
    await runtime.queue.put(stranger)
    await asyncio.wait_for(stranger.sent.wait(), 3)
    assert "No matching approval" in stranger.replies[0]
    assert runtime.responses == []
    assert len(runtime.broker._pending) == 1
    assert not runtime.session.steering_instructions
    await runtime.queue.put(message(f"Reject {token}"))
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert runtime.responses[0]["any_rejected"]


async def test_stop_cleans_pending_approval_and_restores_callback(
    running_processor: SimpleNamespace,
) -> None:
    runtime = running_processor
    origin = message("perform task")
    await runtime.queue.put(origin)
    await asyncio.wait_for(origin.sent.wait(), 3)
    token = nonce(origin)
    await runtime.queue.put(message("/stop"))
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert not runtime.broker._pending
    assert runtime.future.done()
    assert runtime.future.result()["any_rejected"]
    assert runtime.session._remote_approval_handler is runtime.previous_handler
    assert not runtime.session._remote_message_lock.locked()
    late = message(f"Approve {token}")
    await runtime.queue.put(late)
    await asyncio.wait_for(late.sent.wait(), 3)
    assert "No matching approval" in late.replies[0]


async def test_plan_mode_write_denial_precedes_remote_approval(
    running_processor: SimpleNamespace,
) -> None:
    runtime = running_processor
    runtime.session.plan_mode_enabled = True
    runtime.payload = {
        "action_requests": [{"name": "write_file", "args": {"file_path": "source.py"}}]
    }
    origin = message("perform task")
    await runtime.queue.put(origin)
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert runtime.responses[0]["any_rejected"]
    assert runtime.responses[0]["decisions"][0]["type"] == "reject"
    assert not runtime.broker._pending
    assert "Approve " not in "\n".join(origin.replies)


async def test_callback_failure_rejects_event_and_restores_handler(
    running_processor: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = running_processor
    monkeypatch.setattr(runtime.broker, "request", AsyncMock(side_effect=RuntimeError("failed")))
    await runtime.queue.put(message("perform task"))
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert runtime.future.done()
    assert runtime.future.result()["any_rejected"]
    assert runtime.session._remote_approval_handler is runtime.previous_handler


@pytest.mark.parametrize("mutate_payload", [False, True])
async def test_saved_plan_approval_binds_original_interrupt_to_displayed_snapshot(
    running_processor: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_payload: bool,
) -> None:
    from unittest.mock import Mock

    runtime = running_processor
    runtime.kind = "plan"
    runtime.payload = {"plan": ""}
    saved = ["Plan A: update the parser."]
    resolver = Mock(side_effect=lambda *_args, **_kwargs: (saved[0], None))
    monkeypatch.setattr("novacode_cli.ui.interrupt_handlers.resolve_plan_content", resolver)
    request = AsyncMock(wraps=runtime.broker.request)
    monkeypatch.setattr(runtime.broker, "request", request)
    origin = message("perform task")
    await runtime.queue.put(origin)
    await asyncio.wait_for(origin.sent.wait(), 3)
    assert runtime.payload["plan"] == "Plan A: update the parser."
    assert request.call_args.args[2] is runtime.payload
    assert "Plan A" in "\n".join(origin.replies)
    saved[0] = "Plan B: change the permissions."
    if mutate_payload:
        runtime.payload["plan"] = saved[0]
    await runtime.queue.put(message(f"Approve {nonce(origin)}"))
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert runtime.responses[0]["response"]["approved"] is not mutate_payload
    if not mutate_payload:
        assert runtime.payload["plan"] == "Plan A: update the parser."
    resolver.assert_called_once()


@pytest.mark.parametrize("malformed", [None, [], "Plan text without a payload"])
async def test_malformed_plan_payload_cannot_approve_an_unbound_snapshot(
    running_processor: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
) -> None:
    from unittest.mock import Mock

    runtime = running_processor
    runtime.kind = "plan"
    runtime.payload = malformed
    resolver = Mock(return_value=("A saved plan", None))
    monkeypatch.setattr("novacode_cli.ui.interrupt_handlers.resolve_plan_content", resolver)
    origin = message("perform task")
    await runtime.queue.put(origin)
    await asyncio.wait_for(runtime.queue.join(), 3)
    assert runtime.responses[0]["response"]["approved"] is False
    assert not runtime.broker._pending
    assert "Approve " not in "\n".join(origin.replies)
    resolver.assert_not_called()
