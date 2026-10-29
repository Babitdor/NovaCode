"""Regression proof for passing-mode persistence, deletion, notices and command ownership."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli import handoff
from novacode_cli import ui_events as ev
from novacode_cli.agents.handoff import HandoffMiddleware
from novacode_cli.commands import CommandRegistry
from novacode_cli.commands.handoff_handler import handle_passing_command, register_commands
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.handoff import HandoffMemory, HandoffRun, prepare
from tests.test_handoff import state
from tests.test_prime import Model
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.store.base import BaseStore


class FailDeleteOnce:
    """Simulate a transient deletion failure after the durable marker is written."""

    def __init__(self, store: BaseStore) -> None:
        self.store = store
        self.failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.store, name)

    def delete(self, namespace: tuple[str, ...], identity: str) -> None:
        if not self.failed:
            self.failed = True
            message = "simulated transient deletion failure"
            raise OSError(message)
        self.store.delete(namespace, identity)


def test_partial_deletion_is_visible_for_cleanup_and_retryable(tmp_path: Path):
    run = HandoffRun(state(tmp_path), "Portfolio")
    memory = run.memory
    original = memory.store
    memory.store = FailDeleteOnce(original)
    with pytest.raises(OSError, match="transient deletion failure"):
        memory.delete(run.record["id"])
    assert memory.get(run.record["id"]) is None, "Deletion-pending data must not be resumed"
    rows = memory.records()
    assert len(rows) == 1
    assert rows[0]["deletion_pending"]
    assert memory.delete(run.record["id"])
    assert original.get(memory.namespace, run.record["id"]) is None
    assert memory.records() == []
    assert not memory.save(run.record), "A late writer must not resurrect deleted content"


def test_partial_cli_deletion_can_be_confirmed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session = state(tmp_path)
    run = HandoffRun(session, "Portfolio")
    proxy = FailDeleteOnce(run.memory.store)
    monkeypatch.setattr(handoff, "get_durable_store", lambda: proxy)
    identity = run.record["id"]
    assert "Confirm deletion" in handle_passing_command(session, f"delete {identity}")
    assert "storage unavailable" in handle_passing_command(session, f"delete {identity} confirm")
    assert "Handoff deleted" in handle_passing_command(session, f"delete {identity} confirm")
    assert session.handoff_latest is None
    assert proxy.get(run.memory.namespace, identity) is None


@pytest.mark.parametrize("kind", ["snapshot", "inherited", "document"])
async def test_notices_distinguish_new_skill_documents_from_fallbacks(tmp_path: Path, kind: str):
    session = state(tmp_path)
    if kind == "inherited":
        previous = HandoffRun(session, "Earlier task")
        previous.save_document("Earlier task", "# Previous handoff document")
        previous.finish("complete")
    responses = []
    if kind == "document":
        responses.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff_save",
                        "id": "doc",
                        "args": {"title": "Portfolio", "content": "# New handoff"},
                    }
                ],
            )
        )
    responses.append(AIMessage("Finished this turn"))
    graph = create_agent(
        Model(responses=responses), middleware=[HandoffMiddleware()], checkpointer=InMemorySaver()
    )
    events = [e async for e in iterate_agent_events("Portfolio", graph, "nova-agent", session)]
    assert not any(isinstance(e, ev.Error) for e in events)
    notice = next(e for e in reversed(events) if isinstance(e, ev.ContextMessage) and e.record_id)
    expected = "document" if kind == "document" else "snapshot"
    assert notice.data["summary_status"] == expected
    assert session.handoff_latest["summary_status"] == expected
    if expected == "snapshot":
        assert "snapshot" in notice.message.lower()
        assert "did not produce a new document" in notice.message
    else:
        assert "document saved" in notice.message


def test_passing_command_does_not_claim_the_handoff_skill_name():
    registry = CommandRegistry()
    register_commands(registry)
    assert registry.get("passing") is not None
    assert registry.get("handoff") is None


@pytest.mark.timeout(30)
@pytest.mark.parametrize("failure", ["write", "read"])
async def test_failed_old_chat_save_blocks_fresh_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
):
    app = _app()
    session = app.session_state
    session.workspace_root = str(tmp_path)
    run = HandoffRun(session, "Previous handoff")
    run.save_document("Previous handoff", "# Earlier summary")
    run.finish("complete")
    monkeypatch.setattr(app, "_consolidate_learning", AsyncMock())
    reader = AsyncMock(
        return_value=SimpleNamespace(values={"messages": [HumanMessage("New unsaved work")]})
    )
    if failure == "read":
        reader.side_effect = TimeoutError("simulated checkpointer timeout")
    monkeypatch.setattr(app, "_read_state", reader)
    dispatch = Mock()
    monkeypatch.setattr(app, "_dispatch_root", dispatch)
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        old_thread, old_session = session.thread_id, session.session_id
        from novacode_cli.tui.widgets import PromptInput

        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "Keep my draft"
        save = Mock(side_effect=OSError("simulated disk failure"))
        app.session_manager = SimpleNamespace(save_session=save, sessions_dir=tmp_path / "sessions")
        monkeypatch.setattr(app, "_run_skill", AsyncMock(return_value=False))
        monkeypatch.setattr(app, "_run_plugin_command", AsyncMock(return_value=False))
        await app._run_slash("/passing")
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        await pilot.click("#handoff-continue")
        await pilot.pause()
        assert session.thread_id == old_thread
        assert session.session_id == old_session
        assert session.handoff_latest["id"] == run.record["id"]
        assert session.handoff_pending is None
        assert prompt.text == "Keep my draft"
        assert not prompt.disabled
        dispatch.assert_not_called()
        app.session_manager = None


@pytest.mark.timeout(30)
async def test_bare_handoff_routes_to_skill_instead_of_browser(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    skill = AsyncMock(return_value=True)
    monkeypatch.setattr(app, "_run_skill", skill)
    monkeypatch.setattr(app, "_run_plugin_command", AsyncMock(return_value=False))
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        await app._run_slash("/handoff focus on remaining portfolio work")
        skill.assert_awaited_once_with("/handoff focus on remaining portfolio work")


@pytest.mark.timeout(30)
async def test_successful_save_keeps_previous_chat_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.session.session_persistence import SessionManager

    app = _app()
    session = app.session_state
    session.workspace_root = str(tmp_path)
    old_thread, old_id = session.thread_id, session.session_id
    run = HandoffRun(session, "Saved handoff")
    run.save_document("Saved handoff", "# Context for the next session")
    run.finish("complete")

    async def read_state(_agent: object, config: dict, **_kwargs: object) -> SimpleNamespace:
        messages = (
            [HumanMessage("Preserve this conversation")]
            if config["configurable"]["thread_id"] == old_thread
            else []
        )
        return SimpleNamespace(values={"messages": messages})

    monkeypatch.setattr(app, "_read_state", read_state)
    monkeypatch.setattr(app, "_consolidate_learning", AsyncMock())
    dispatch = Mock()
    monkeypatch.setattr(app, "_dispatch_root", dispatch)
    manager = SessionManager(tmp_path / "sessions")
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        app.session_manager = manager
        await app._run_slash("/passing")
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        await pilot.click("#handoff-continue")
        await pilot.pause()
        assert session.thread_id != old_thread
        previous = manager.load_session(old_id)
        assert previous is not None
        assert previous.messages[0].content == "Preserve this conversation"
        assert not previous.meta.cleared
        assert session.handoff_pending["id"] == run.record["id"]
        dispatch.assert_called_once()


@pytest.mark.timeout(30)
async def test_browser_reloads_partial_delete_for_cleanup_without_allowing_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from textual.widgets import Button

    app = _app()
    app.session_state.workspace_root = str(tmp_path)
    run = HandoffRun(app.session_state, "Delete this handoff")
    proxy = FailDeleteOnce(run.memory.store)
    monkeypatch.setattr(handoff, "get_durable_store", lambda: proxy)
    with pytest.raises(OSError, match="transient deletion failure"):
        HandoffMemory(str(tmp_path)).delete(run.record["id"])
    assert "not found" in prepare(app.session_state, run.record["id"])
    async with app.run_test(size=(55, 24)) as pilot:
        await pilot.pause()
        await app._run_slash("/passing")
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        assert app.screen.query_one("#handoff-continue", Button).disabled
        assert not app.screen.query_one("#handoff-delete", Button).disabled
        await pilot.click("#handoff-delete")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        assert proxy.get(run.memory.namespace, run.record["id"]) is None
