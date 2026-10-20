"""Cross-tab delivery must not cancel, switch, or overwrite another session."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from novacode_cli.sessions.gateway import SendMessage, endpoint
from novacode_cli.sessions.tui_messaging import SessionCommunication
from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401


async def setup(app, monkeypatch):
    pane = await _add_pane(app, sid="b", title="backend")
    pane.child = SimpleNamespace(worktree="/backend")
    pane.state["session_state"] = SimpleNamespace(auto_approve=False)
    send = AsyncMock(return_value="p-peer")
    supervisor = SimpleNamespace(send_prompt=send, cancel=AsyncMock())
    monkeypatch.setattr(app, "_supervisor", lambda: supervisor)
    controller = SessionCommunication(app)
    return pane, send, controller


async def test_cross_tab_request_preserves_sender_turn_and_target_approval(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        app._turn_active = True
        app._root_pane.status = "running"
        active = app._active_pane
        token = endpoint.set(controller.gateway("root"))
        try:
            result = await SendMessage.ainvoke(
                {"recipient": "backend", "content": "Explain login"}
            )
        finally:
            endpoint.reset(token)
        for _ in range(40):
            if send.await_count:
                break
            await asyncio.sleep(0.05)
        assert send.await_count == 1
        assert send.call_args.kwargs["auto_approve"] is False
        assert app._turn_active and app._active_pane is active
        controller.child_done("b", "wrong-id", True)
        assert controller.broker.messages[result["id"]].status == "running"
        controller.child_answer("b", "Login uses tokens")
        controller.child_done("b", "p-peer", True)
        for _ in range(40):
            if controller.broker.messages[result["id"]].status == "completed":
                break
            await asyncio.sleep(0.05)
        replies = await controller.handle("root", "read", {})
        assert replies["messages"][0]["response"] == "Login uses tokens"
        assert pane.state["session_state"].auto_approve is False
        await controller.close()


async def test_busy_target_waits_and_foreign_reply_is_rejected(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        pane.status = "running"
        result = await controller.handle("root", "send", {"target": "b", "text": "Question"})
        await asyncio.sleep(0.15)
        send.assert_not_awaited()
        assert "error" in await controller.handle(
            "root", "reply", {"id": result["id"], "text": "forged"}
        )
        pane.status = "idle"
        for _ in range(40):
            if send.await_count:
                break
            await asyncio.sleep(0.05)
        assert send.await_count == 1
        await controller.close()
        assert not controller.broker.messages


async def test_routed_input_during_turn_never_becomes_local_steering(monkeypatch):
    app = _app()
    async with app.run_test() as pilot:
        pane, send, controller = await setup(app, monkeypatch)
        app._session_communication = controller
        monkeypatch.setattr(app, "_get_agent_names", lambda: [])
        steer = AsyncMock()
        monkeypatch.setattr(app, "_add_live_steer", steer)
        app._turn_active = True
        inp = app.query_one("#prompt")
        inp.value = "@tab:b Explain login"
        app.on_input_submitted(SimpleNamespace(input=inp, value=inp.value))
        for _ in range(40):
            await pilot.pause(0.05)
            if send.await_count:
                break
        assert send.await_count == 1
        steer.assert_not_called()
        assert app._active_pane is app._root_pane
        await controller.close()
