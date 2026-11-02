"""Remote decisions resolve the correct TUI interrupt without changing approval mode."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from types import MethodType, SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.remote.bridge import RemoteMessage, RemotePlatform
from novacode_cli.tui.app import NovaApp


def origin(
    platform: RemotePlatform = RemotePlatform.TELEGRAM,
) -> tuple[RemoteMessage, asyncio.Event, list[str], AsyncMock]:
    shown = asyncio.Event()
    prompts = []
    retract = AsyncMock()

    async def ask(text: str) -> AsyncMock:
        prompts.append(text)
        if "Approve " in text:
            shown.set()
        return retract

    msg = RemoteMessage(
        platform, 7, "owner", "work", AsyncMock(), sender_id="42", thread_id=9, ask_fn=ask
    )
    return msg, shown, prompts, retract


def tool_request() -> dict:
    return {
        "requires_owner_approval": True,
        "action_requests": [
            {"name": "shell", "args": {"command": "echo reviewed"}},
        ],
    }


def app_for(msg: RemoteMessage | None = None) -> SimpleNamespace:
    state = SimpleNamespace(auto_approve=False, plan_mode_enabled=False, plan_agent=None)
    app = SimpleNamespace(
        session_state=state,
        backend=None,
        _remote_msg=msg,
        _remote_approvals=None,
        _active_pane=None,
        _local_remote_streams=[],
        notify=Mock(),
        push_screen_wait=AsyncMock(return_value="approve"),
        _update_mode_badge=Mock(),
        _finish_remote_mirrors=AsyncMock(),
        _sync_child_activity=Mock(),
        _refresh_tabs=Mock(),
    )
    app._handle_interrupt_inner = MethodType(NovaApp._handle_interrupt_inner, app)
    app._handle_interrupt = MethodType(NovaApp._handle_interrupt, app)
    return app


async def decide(
    app: SimpleNamespace,
    msg: RemoteMessage,
    prompts: list[str],
    verb: str = "Approve",
    *,
    sender_id: str | None = None,
    thread_id: int | None = None,
) -> None:
    nonce = re.search(r"Approve ([a-f0-9]+)", prompts[-1])[1]
    response = RemoteMessage(
        msg.platform,
        msg.chat_id,
        "owner",
        f"{verb} {nonce}",
        AsyncMock(),
        sender_id=msg.sender_id if sender_id is None else sender_id,
        thread_id=msg.thread_id if thread_id is None else thread_id,
    )
    assert await NovaApp._remote_route(app, response)


@pytest.mark.parametrize("platform", [RemotePlatform.TELEGRAM, RemotePlatform.DISCORD])
@pytest.mark.parametrize("verb", ["Approve", "Reject"])
async def test_remote_tool_waits_for_scoped_decision_and_never_enables_auto(
    platform: RemotePlatform,
    verb: str,
) -> None:
    msg, shown, prompts, retract = origin(platform)
    app = app_for(msg)
    # A genuine owner ask remains interactive even if explicit auto mode is on.
    app.session_state.auto_approve = True
    event = ev.InterruptRequest("tool", tool_request(), asyncio.get_running_loop().create_future())
    task = asyncio.create_task(NovaApp._handle_interrupt(app, event))
    await asyncio.wait_for(shown.wait(), 2)
    assert not task.done()
    assert not event.future.done()
    assert "echo reviewed" in prompts[-1]
    await decide(app, msg, prompts, sender_id="other")
    await decide(app, msg, prompts, thread_id=10)
    assert not task.done()
    assert not event.future.done()
    await decide(app, msg, prompts, verb)
    await asyncio.wait_for(task, 2)
    assert event.future.result()["any_rejected"] is (verb == "Reject")
    app.push_screen_wait.assert_not_awaited()
    assert app.session_state.auto_approve is True
    retract.assert_awaited_once()
    assert not app._remote_approvals._pending


async def test_remote_plan_shows_content_and_approves_in_manual_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = ["Plan: inspect then test"]
    monkeypatch.setattr(
        "novacode_cli.ui.interrupt_handlers.resolve_plan_content",
        lambda *_args, **_kwargs: (saved[0], None),
    )
    msg, shown, prompts, _retract = origin()
    app = app_for(msg)
    app.session_state.plan_mode_enabled = True
    event = ev.InterruptRequest("plan", {}, asyncio.get_running_loop().create_future())
    task = asyncio.create_task(NovaApp._handle_interrupt(app, event))
    await asyncio.wait_for(shown.wait(), 2)
    assert "Plan: inspect then test" in prompts[-1]
    saved[0] = "A different, unreviewed plan"
    await decide(app, msg, prompts)
    await asyncio.wait_for(task, 2)
    assert event.future.result() == {
        "response": {"approved": True, "mode": "manual"},
        "state_update": {"plan_mode_enabled": False},
    }
    assert app.session_state.auto_approve is False
    assert app.session_state.plan_mode_enabled is False
    assert event.payload["plan"] == "Plan: inspect then test"


async def test_remote_plan_mode_denial_cannot_be_approved():
    msg, _shown, prompts, _retract = origin()
    app = app_for(msg)
    app.session_state.plan_mode_enabled = True
    request = {
        "action_requests": [
            {
                "name": "write_file",
                "args": {
                    "file_path": "/outside.py",
                    "content": "unsafe",
                },
            }
        ],
        "requires_owner_approval": True,
    }
    event = ev.InterruptRequest("tool", request, asyncio.get_running_loop().create_future())
    await NovaApp._handle_interrupt(app, event)
    assert event.future.result()["any_rejected"]
    assert not prompts
    assert app._remote_approvals is None


async def test_remote_child_never_uses_parent_workspace_as_plan_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = Mock(return_value=("Wrong workspace's plan", None))
    monkeypatch.setattr("novacode_cli.ui.interrupt_handlers.resolve_plan_content", resolve)
    msg, _shown, prompts, _retract = origin()
    app = app_for()
    child_state = SimpleNamespace(plan_mode_enabled=True, auto_approve=False)
    pane = SimpleNamespace(
        kind="child",
        sid="child",
        state={"session_state": child_state},
        remote_turns=deque([SimpleNamespace(msg=msg)]),
    )
    event = ev.InterruptRequest("plan", {}, asyncio.get_running_loop().create_future())
    await NovaApp._handle_interrupt(app, event, pane=pane)
    assert not event.future.result()["response"]["approved"]
    assert child_state.plan_mode_enabled
    assert not prompts
    resolve.assert_not_called()


async def test_terminal_turn_keeps_local_approval_even_with_remote_mirror():
    msg, _shown, prompts, _retract = origin()
    app = app_for()
    app._local_remote_streams = [(msg, None, None)]
    event = ev.InterruptRequest("tool", tool_request(), asyncio.get_running_loop().create_future())
    await NovaApp._handle_interrupt(app, event)
    app.push_screen_wait.assert_awaited_once()
    assert event.future.result()["decisions"] == [{"type": "approve"}]
    assert not prompts
    assert app._remote_approvals is None
    assert app.session_state.auto_approve is False


@pytest.mark.parametrize("finish_before_reply", [False, True])
async def test_background_child_approval_does_not_swap_root_state(
    *,
    finish_before_reply: bool,
) -> None:
    msg, shown, prompts, retract = origin(RemotePlatform.DISCORD)
    app = app_for()
    root_state = app.session_state
    child_state = SimpleNamespace(auto_approve=False, plan_mode_enabled=False)
    pane = SimpleNamespace(
        sid="child",
        title="child",
        kind="child",
        status="needs-approval",
        state={"session_state": child_state},
        remote_turns=deque([SimpleNamespace(msg=msg)]),
        pending_interrupt={"id": "i1", "kind": "tool", "payload": tool_request()},
    )
    supervisor = SimpleNamespace(reply_interrupt=AsyncMock())
    app._supervisor = lambda: supervisor
    handler = NovaApp._handle_child_interrupt.__wrapped__
    task = asyncio.create_task(handler(app, pane))
    await asyncio.wait_for(shown.wait(), 2)
    assert app.session_state is root_state
    supervisor.reply_interrupt.assert_not_awaited()
    if finish_before_reply:
        pane.status = "idle"
        pane.remote_turns.clear()
        await NovaApp._finish_remote_turn(app, pane)
    else:
        await decide(app, msg, prompts)
    await asyncio.wait_for(task, 2)
    assert app.session_state is root_state
    assert not child_state.auto_approve
    assert not root_state.auto_approve
    if finish_before_reply:
        supervisor.reply_interrupt.assert_not_awaited()
        assert pane.status == "idle"
    else:
        supervisor.reply_interrupt.assert_awaited_once_with(
            "child",
            "i1",
            {
                "decisions": [{"type": "approve"}],
                "any_rejected": False,
            },
        )
    retract.assert_awaited_once()
    assert not app._remote_approvals._pending


async def test_remote_approval_cancellation_retracts_and_rejects():
    msg, shown, _prompts, retract = origin()
    app = app_for(msg)
    event = ev.InterruptRequest("tool", tool_request(), asyncio.get_running_loop().create_future())
    task = asyncio.create_task(NovaApp._handle_interrupt(app, event))
    await asyncio.wait_for(shown.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert event.future.result()["any_rejected"]
    retract.assert_awaited_once()
    assert not app._remote_approvals._pending


async def test_hidden_root_approval_uses_root_policy_without_switching_visible_tab():
    msg, shown, prompts, _retract = origin()
    app = app_for(msg)
    root_state = app.session_state
    visible_state = SimpleNamespace(plan_mode_enabled=True, auto_approve=False)
    app.session_state = visible_state
    visible = SimpleNamespace(sid="visible-child")
    app._active_pane = visible
    app._feed_remote = Mock()
    root = SimpleNamespace(
        kind="root",
        sid="root",
        status="running",
        state={
            "session_state": root_state,
            "backend": None,
        },
    )
    request = {
        "requires_owner_approval": True,
        "action_requests": [
            {
                "name": "write_file",
                "args": {"file_path": "/code.py", "content": "result"},
            }
        ],
    }
    event = ev.InterruptRequest("tool", request, asyncio.get_running_loop().create_future())
    task = asyncio.create_task(NovaApp._deliver(app, root, event))
    await asyncio.wait_for(shown.wait(), 2)
    assert not event.future.done()
    await decide(app, msg, prompts)
    await asyncio.wait_for(task, 2)
    assert event.future.result()["decisions"] == [{"type": "approve"}]
    assert app._active_pane is visible
    assert app.session_state is visible_state
    assert visible_state.plan_mode_enabled
    assert not root_state.auto_approve


@pytest.mark.parametrize("verb", ["Approve", "Reject"])
@pytest.mark.parametrize("include_denied", [False, True])
async def test_real_agent_tool_executes_only_after_remote_approval(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    *,
    include_denied: bool,
) -> None:
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.core.agent_loop import iterate_agent_events

    performed = []

    @tool
    def checked_action(value: int) -> str:
        """Record an action that requires the owner's approval."""
        performed.append(value)
        return "recorded"

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, _tools: list, **_kwargs: object) -> Self:
            return self

    calls = [
        {
            "name": "checked_action",
            "id": "call-1",
            "args": {"value": 7},
        }
    ]
    if include_denied:
        calls.insert(0, {"name": "checked_action", "id": "denied", "args": {"value": 8}})
    model = Model(responses=[AIMessage("", tool_calls=calls), AIMessage("finished")])
    agent = create_agent(
        model,
        tools=[checked_action],
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"checked_action": True})],
        checkpointer=InMemorySaver(),
    )
    monkeypatch.setattr(
        "novacode_cli.ui.hitl_approval.evaluate_tool_actions",
        lambda request, *_args, **_kwargs: [
            {"type": "reject", "message": "Policy denial"}
            if action["args"].get("value") == 8
            else None
            for action in request["action_requests"]
        ],
    )
    msg, shown, prompts, _retract = origin()
    app = app_for(msg)
    app.session_state.thread_id = "remote-tool-gate"
    app.session_state.active_goal = None

    async def run() -> None:
        async for event in iterate_agent_events(
            "record the value", agent, "nova-agent", app.session_state
        ):
            if isinstance(event, ev.InterruptRequest):
                await NovaApp._handle_interrupt(app, event)

    task = asyncio.create_task(run())
    await asyncio.wait_for(shown.wait(), 3)
    assert performed == []
    assert not task.done()
    await decide(app, msg, prompts, verb)
    await asyncio.wait_for(task, 3)
    assert performed == ([7] if verb == "Approve" else [])
    assert not app.session_state.auto_approve
