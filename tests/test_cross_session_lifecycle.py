"""Race, hidden-root, cancellation and protocol ownership regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from novacode_cli import ui_events as ev
from novacode_cli.sessions.gateway import request
from novacode_cli.sessions.messaging import SessionBroker
from tests.test_cross_session_tui import setup
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_fast_child_reply_during_send_and_fifo(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        delivered = []

        async def immediate(sid, text, **kwargs):
            delivered.append(text)
            pid = kwargs["prompt_id"]
            controller.child_answer(sid, "wrong", "foreign")
            controller.child_answer(sid, "correct", pid)
            controller.child_done(sid, pid, True)
            await asyncio.sleep(0)
            return pid

        send.side_effect = immediate
        results = await asyncio.gather(
            *(controller.handle("root", "send", {"target": "b", "text": f"Q{i}"}) for i in range(3))
        )
        await wait_until(
            lambda: all(
                controller.broker.messages[result["id"]].status == "completed" for result in results
            )
        )
        assert [text.rsplit("\n", 1)[-1] for text in delivered] == ["Q0", "Q1", "Q2"]
        assert all(
            controller.broker.messages[result["id"]].response == "correct" for result in results
        )
        assert not controller.child_turns and not controller.active_depth
        await controller.close()


async def test_hidden_root_uses_own_state_and_buffers_events(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        root = app._root_pane
        own_agent, own_backend, own_state = app.agent, app.backend, app.session_state
        await app._switch_to(pane)
        pane.status = "running"
        app._turn_active = True
        observed = []

        async def stream(text, agent, assistant_id, state, **kwargs):
            observed.append((agent, state, kwargs["backend"], await request("list", {})))
            yield ev.AssistantMessage(text="root response", agent_name="Nova", agent_color="cyan")
            yield ev.Done()

        monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", stream)
        monkeypatch.setattr(app, "_save_session", AsyncMock())
        result = await controller.handle(
            "b", "send", {"target": "root", "text": "Explain main project"}
        )
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "completed")
        assert observed[0][:3] == (own_agent, own_state, own_backend)
        assert app._active_pane is pane and app._turn_active
        assert root.state["_turn_active"] is False
        assert any(
            isinstance(event, ev.AssistantMessage) and event.text == "root response"
            for event in root.buffer
        )
        assert controller.broker.messages[result["id"]].response == "root response"
        await controller.close()


async def test_cancel_exit_and_reply_budget_never_wedge(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        result = await controller.handle("root", "send", {"target": "b", "text": "Question"})
        await wait_until(lambda: bool(controller.child_turns))
        controller.cancel("b")
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "cancelled")
        assert not controller.child_turns
        controller.broker = SessionBroker(max_bytes=12)
        controller.refresh()
        next_result = await controller.handle("root", "send", {"target": "b", "text": "Q"})
        await wait_until(lambda: bool(controller.child_turns))
        controller.child_answer("b", "a" * 50)
        controller.child_done("b", "p-peer", True)
        await wait_until(lambda: controller.broker.messages[next_result["id"]].status == "failed")
        assert controller.broker.retained_bytes <= 12
        await controller.close()


async def test_peer_completion_cannot_finish_queued_telegram_turn(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        app._session_communication = controller
        pane.remote_turns.append(
            SimpleNamespace(prompt_id="remote-other", answer="", status=None, answer_stream=None)
        )
        finish = AsyncMock()
        monkeypatch.setattr(app, "_finish_remote_turn", finish)
        await app._on_child_message("b", {"t": "turn_done", "id": "peer-m1", "ok": True})
        finish.assert_not_awaited()
        pane.event_prompt_id = "peer-m1"
        app._feed_remote(
            pane, ev.AssistantMessage(text="peer answer", agent_name="Nova", agent_color="cyan")
        )
        assert pane.remote_turns[0].answer == ""
        await app._on_child_message("b", {"t": "turn_done", "id": "remote-other", "ok": True})
        finish.assert_awaited_once()
        await controller.close()
