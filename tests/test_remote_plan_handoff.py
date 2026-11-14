"""Plan decisions race safely across terminal/chat; execution keeps the chat identity."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.states.Session import SessionState
from novacode_cli.tui.app import NovaApp
from tests.test_remote_approval_tui import app_for, decide, origin
from tests.test_tui_sessions import isolated_session_config  # noqa: F401

PLAN = "# Plan\n## Steps\n1. Fix the parser\n## Verification\n- Run parser tests"


@pytest.mark.parametrize("platform", [RemotePlatform.TELEGRAM, RemotePlatform.DISCORD])
@pytest.mark.parametrize(
    ("winner", "choice"),
    [
        ("remote", "Approve"),
        ("remote", "Reject"),
        ("terminal", "manual"),
        ("terminal", "auto"),
        ("terminal", "refine"),
    ],
)
async def test_first_plan_decision_retracts_other_surface(
    monkeypatch: pytest.MonkeyPatch, platform: RemotePlatform, winner: str, choice: str
):
    monkeypatch.setattr(
        "novacode_cli.ui.interrupt_handlers.resolve_plan_content", lambda *_a, **_kw: (PLAN, None)
    )
    msg, shown, prompts, retract = origin(platform)
    app = app_for(msg)
    app._log = Mock()
    terminal = asyncio.get_running_loop().create_future()

    async def local(_screen: object) -> str:
        return await terminal

    app.push_screen_wait = AsyncMock(side_effect=local)
    app.session_state.plan_mode_enabled = True
    event = ev.InterruptRequest("plan", {}, asyncio.get_running_loop().create_future())
    task = asyncio.create_task(NovaApp._handle_interrupt(app, event))
    await asyncio.wait_for(shown.wait(), 2)
    app.push_screen_wait.assert_awaited_once()
    assert PLAN.replace("\n", r"\n") in prompts[-1]
    if winner == "remote":
        await decide(app, msg, prompts, choice)
    else:
        terminal.set_result(choice)
    await asyncio.wait_for(task, 2)
    approved = choice in ("Approve", "auto", "manual")
    assert event.future.result()["response"]["approved"] is approved
    assert app.session_state.plan_mode_enabled is (not approved)
    assert app.session_state.auto_approve is (choice == "auto")
    if approved:
        assert event.future.result()["response"]["mode"] == (
            "manual" if winner == "remote" else choice
        )
    assert not app._remote_approvals._pending
    retract.assert_awaited_once()
    result = event.future.result()
    # A late remote reply cannot approve or change a plan already settled locally.
    await decide(app, msg, prompts, "Approve")
    assert event.future.result() == result


async def test_plan_handoff_keeps_durable_session_and_live_bridge():
    session = SessionState()
    session.set_approved_plan(PLAN)
    session.plan_mode_enabled = True
    session._remote_message_queue = asyncio.Queue()
    session._remote_bridge_manager = object()
    session.remote_connections = [{"platform": "telegram", "chat_id": "7"}]
    session.steering_instructions.append("old planning context")
    old_id, old_thread = session.session_id, session.thread_id
    manager, queue, lock = (
        session._remote_bridge_manager,
        session._remote_message_queue,
        session._remote_message_lock,
    )
    streamed = []

    async def implement(prompt: str) -> None:
        streamed.append(prompt)
        assert session.session_id == old_id
        assert session.thread_id != old_thread
        assert session._remote_bridge_manager is manager
        assert session._remote_message_queue is queue
        assert session._remote_message_lock is lock
        assert not session.steering_instructions
        assert not session.plan_mode_enabled

    app = SimpleNamespace(
        session_state=session,
        agent=SimpleNamespace(
            aupdate_state=AsyncMock(),
            aget_state=AsyncMock(return_value=SimpleNamespace(values={"todos": []})),
        ),
        _transcript=lambda: SimpleNamespace(remove_children=AsyncMock()),
        _show_home_banner=Mock(),
        _log=Mock(),
        _paint_todos=Mock(),
        _stream_prompt=implement,
        _plan_scoped_auto_approve=False,
    )
    await NovaApp._maybe_run_approved_plan(app)
    assert len(streamed) == 1
    assert PLAN in streamed[0]
    assert session.remote_connections == [{"platform": "telegram", "chat_id": "7"}]
    assert not app._turn_active


@pytest.mark.parametrize("winner", ["remote", "terminal"])
async def test_real_terminal_modal_is_removed_after_either_decision(
    monkeypatch: pytest.MonkeyPatch, winner: str
):
    from novacode_cli.tui.screens import PlanApprovalModal
    from tests.test_tui_sessions import _app

    monkeypatch.setattr(
        "novacode_cli.ui.interrupt_handlers.resolve_plan_content", lambda *_a, **_kw: (PLAN, None)
    )
    app = _app()
    msg, shown, prompts, retract = origin()
    app._remote_msg = msg
    event = ev.InterruptRequest("plan", {}, asyncio.get_running_loop().create_future())
    async with app.run_test(size=(100, 36)) as pilot:
        app.run_worker(app._handle_interrupt(event))
        await asyncio.wait_for(shown.wait(), 2)
        await pilot.pause()
        assert isinstance(app.screen, PlanApprovalModal)
        if winner == "remote":
            await decide(app, msg, prompts)
        else:
            await pilot.press("m")
        await asyncio.wait_for(asyncio.shield(event.future), 2)
        await pilot.pause()
        assert event.future.result()["response"]["approved"]
        assert not isinstance(app.screen, PlanApprovalModal)
        assert not app._remote_approvals._pending
        retract.assert_awaited_once()


async def test_remote_consumer_reads_implementation_thread_then_handles_next_message(
    monkeypatch: pytest.MonkeyPatch,
):
    from langchain_core.messages import AIMessage

    from tests.test_tui_sessions import _app

    app = _app()
    queue = asyncio.Queue()
    app.session_state._remote_message_queue = queue
    app.session_state.thread_id = "planning-thread"
    prompts = []

    async def state(config: dict) -> SimpleNamespace:
        text = (
            "Implementation reply"
            if config["configurable"]["thread_id"] == "implementation-thread"
            else "Old planning reply"
        )
        return SimpleNamespace(values={"messages": [AIMessage(content=text)]})

    async def stream(text: str) -> None:
        prompts.append(text)
        app.session_state.thread_id = "implementation-thread"

    monkeypatch.setattr(app.agent, "aget_state", state)
    monkeypatch.setattr(app, "_stream_prompt", stream)
    async with app.run_test(size=(100, 36)):
        app._remote_consumer()
        first, *_ = origin()
        first.thread_id = None
        first.text = "Plan and implement"
        await queue.put(first)
        await asyncio.wait_for(queue.join(), 5)
        assert any(
            "Implementation reply" in call.args[0] for call in first.reply_fn.await_args_list
        )
        assert not any(
            "Old planning reply" in call.args[0] for call in first.reply_fn.await_args_list
        )
        second, *_ = origin()
        second.thread_id = None
        second.text = "Keep going"
        await queue.put(second)
        await asyncio.wait_for(queue.join(), 5)
        assert prompts == ["Plan and implement", "Keep going"]
        assert app.session_state.thread_id == "implementation-thread"
