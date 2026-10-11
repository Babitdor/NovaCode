"""Exercise failure paths and visible-root lifecycle invariants."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.remote.routing import RemoteRouter
from novacode_cli.sessions.messaging import MessageError, SessionBroker
from tests.test_cross_session_lifecycle import wait_until
from tests.test_cross_session_tui import setup
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def test_remote_explicit_ids_and_ambiguous_titles():
    router = RemoteRouter()
    sessions = {"root": "main", "b": "API", "c": "api"}
    msg = SimpleNamespace(text="@tab:c question")
    assert router.resolve(msg, sessions).sid == "c"
    msg.text = "@api question"
    with pytest.raises(MessageError, match="Ambiguous"):
        router.resolve(msg, sessions)
    msg.text = "@tab:missing question"
    with pytest.raises(MessageError, match="Unknown"):
        router.resolve(msg, sessions)
    assert router.match("@tab:b", sessions) == "b"
    assert router.match("api", sessions) is None


def test_remote_explicit_addresses_support_unicode_and_require_a_message():
    router = RemoteRouter()
    sessions = {"root": "main", "b": "😀"}
    assert router.resolve(SimpleNamespace(text="@tab:😀 question"), sessions).sid == "b"
    for text in ("@tab:b", "@tab:"):
        with pytest.raises(MessageError):
            router.resolve(SimpleNamespace(text=text), sessions)


async def test_handle_errors_reply_notice_eviction_and_click(monkeypatch):
    app = _app()
    async with app.run_test() as pilot:
        pane, send, controller = await setup(app, monkeypatch)
        assert "error" in await controller.handle("root", "list", None)
        assert "error" in await controller.handle("missing", "list", {})
        assert "error" in await controller.handle("root", "send", {"target": None, "text": "Q"})
        assert "error" in await controller.handle("root", "bad-method", {})
        assert len((await controller.handle("root", "list", {}))["sessions"]) == 2
        controller.broker = SessionBroker(max_messages=1)
        result = await controller.handle("root", "send", {"target": "b", "text": "Q"})
        await wait_until(lambda: bool(controller.child_turns))
        notice = controller.notices[("root", result["id"])]
        notice.open_tab()
        await wait_until(lambda: app._active_pane is pane)
        reply = await controller.handle("b", "reply", {"id": result["id"], "text": "A"})
        assert reply["status"] == "completed"
        assert (await controller.handle("root", "read", {}))["messages"][0]["response"] == "A"
        controller.child_done("b", "p-peer", True)
        await wait_until(lambda: not controller.child_turns)
        pane.status = "running"
        controller.active_depth["root"] = 8
        assert (
            "chain"
            in (await controller.handle("root", "send", {"target": "b", "text": "Q"}))["error"]
        )
        controller.active_depth.clear()
        queued = await controller.handle("root", "send", {"target": "b", "text": "Q2"})
        controller.refresh()
        await pilot.pause(0.03)
        assert not notice.is_attached
        controller.cancel("b")
        assert controller.broker.messages[queued["id"]].status == "cancelled"
        await controller.close()
        assert "closed" in (await controller.handle("root", "list", {}))["error"]


@pytest.mark.parametrize("outcome", ["answer", "error", "cancelled", "exception"])
async def test_visible_root_plan_selection_lock_and_outcomes(monkeypatch, outcome):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        own_state = app.session_state
        own_state.plan_mode_enabled = True
        own_state.plan_agent = object()
        own_state.plan_backend = object()
        lock = own_state._remote_message_lock = asyncio.Lock()
        await lock.acquire()
        entered = []

        async def stream(text, agent, aid, state, **kwargs):
            entered.append((agent, kwargs["backend"]))
            if outcome == "exception":
                raise RuntimeError("fixture failure")
            if outcome == "answer":
                yield ev.AssistantMessage(text="answer", agent_name="Nova", agent_color="cyan")
            elif outcome == "error":
                yield ev.Error(message="failure")
            else:
                yield ev.Cancelled()

        monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", stream)
        save = AsyncMock()
        monkeypatch.setattr(app, "_save_session", save)
        result = await controller.handle("b", "send", {"target": "root", "text": "Question"})
        await asyncio.sleep(0.05)
        assert not entered
        lock.release()
        await wait_until(
            lambda: (
                controller.broker.messages[result["id"]].status
                in {"completed", "failed", "cancelled"}
            )
        )
        assert entered == [(own_state.plan_agent, own_state.plan_backend)]
        expected = {
            "answer": "completed",
            "error": "failed",
            "cancelled": "cancelled",
            "exception": "failed",
        }[outcome]
        assert controller.broker.messages[result["id"]].status == expected
        assert not app._turn_active and app._root_pane.status == "idle"
        assert not lock.locked()
        save.assert_awaited_once_with(pane=app._root_pane)
        await controller.close()


async def test_failed_send_and_recipient_exit_release_workers(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        send.return_value = None
        result = await controller.handle("root", "send", {"target": "b", "text": "Q"})
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "failed")
        assert pane.status == "idle"
        send.return_value = "p-peer"
        result = await controller.handle("root", "send", {"target": "b", "text": "Q2"})
        await wait_until(lambda: bool(controller.child_turns))
        pane.status = "exited"
        controller.child_exited("b")
        await wait_until(lambda: not controller.child_turns)
        assert controller.broker.messages[result["id"]].status == "failed"
        assert "b" not in controller.workers
        controller.child_done("b", "p-peer", True)
        controller.child_answer("b", "ignored")
        controller.child_exited("b")
        await controller.close()


async def test_tab_completion_preserves_file_and_subagent_mentions(monkeypatch, tmp_path):
    from novacode_cli.config.config import settings

    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        monkeypatch.setattr(settings, "get_workspace_root", lambda: tmp_path)
        monkeypatch.setattr(app, "_get_agent_names", lambda: ["backend"])
        assert "@tab:b" in app._palette_candidates("@tab:")
        assert "@tab:backend" in app._palette_candidates("@tab:back")
        assert app._resolve_tab_mention("@backend question") is None
        monkeypatch.setattr(app, "_get_agent_names", lambda: [])
        (tmp_path / "backend").write_text("file", encoding="utf-8")
        assert app._resolve_tab_mention("@backend question") is None
        assert app._resolve_tab_mention("@tab:b question") == ("b", "question")
        await controller.close()


async def test_hidden_plan_root_and_unavailable_state_fail_safely(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        state = app.session_state
        state.plan_mode_enabled = True
        state.plan_agent, state.plan_backend = object(), object()
        await app._switch_to(pane)
        captured = []

        async def stream(text, agent, aid, own_state, **kwargs):
            captured.append((agent, own_state, kwargs["backend"]))
            yield ev.Done()

        monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", stream)
        monkeypatch.setattr(app, "_save_session", AsyncMock())
        result = await controller.handle("b", "send", {"target": "root", "text": "Q"})
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "completed")
        assert captured == [(state.plan_agent, state, state.plan_backend)]
        root = app._root_pane
        root.state["session_state"] = None
        try:
            with pytest.raises(MessageError, match="unavailable"):
                await controller.run_root(root, controller.broker.messages[result["id"]])
        finally:
            root.state["session_state"] = state
        lookup = app._pane_for
        monkeypatch.setattr(app, "_pane_for", lambda sid: None)
        await controller.notice(controller.broker.messages[result["id"]])
        monkeypatch.setattr(app, "_pane_for", lookup)
        await controller.close()


async def test_root_cancellation_closes_stream_and_keeps_context_isolated(monkeypatch):
    from novacode_cli.sessions.gateway import endpoint

    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        app._session_communication = controller
        entered, closed = asyncio.Event(), asyncio.Event()

        async def stream(*args, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield ev.Done()
            finally:
                closed.set()

        monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", stream)
        monkeypatch.setattr(app, "_save_session", AsyncMock())
        result = await controller.handle("b", "send", {"target": "root", "text": "Q"})
        await asyncio.wait_for(entered.wait(), 3)
        app.action_cancel_turn()
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "cancelled")
        assert closed.is_set()
        assert not app._turn_active
        assert endpoint.get() is None
        await controller.close()
