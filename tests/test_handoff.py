"""Handoffs save real foreground work and load as project context, never authorization."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli import ui_events as ev
from novacode_cli.agents.handoff import HandoffMiddleware, handoff_save
from novacode_cli.commands.handoff_handler import handle_handoff_command
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.handoff import (
    HandoffMemory,
    HandoffRun,
    current_handoff,
    document,
    handoff_payload,
    prepare,
    restore_handoff,
)
from novacode_cli.states.Session import SessionState
from tests.test_prime import Model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def state(tmp_path: Path, name: str = "source", *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        thread_id=name,
        working_directory=str(tmp_path),
        handoff_enabled=enabled,
        handoff_latest=None,
        handoff_pending=None,
        auto_approve=False,
        plan_mode_enabled=False,
        active_goal=None,
    )


def summary(run: HandoffRun) -> None:
    run.summarize(
        "Portfolio accessibility",
        "Make the portfolio usable",
        {
            "progress": ["Implemented the navigation"],
            "decisions": ["Use semantic HTML"],
            "questions": ["Contact endpoint is not configured"],
            "next_steps": ["Run keyboard checks"],
            "references": ["app.py"],
            "suggested_skills": ["textual"],
        },
    )


def test_commands_are_local_and_project_specific(tmp_path: Path):
    session = state(tmp_path, enabled=False)
    assert "off" in handle_handoff_command(session, "status")
    assert "on" in handle_handoff_command(session, "on")
    assert not session.auto_approve
    assert "No saved" in handle_handoff_command(session)
    (tmp_path / "app.py").write_text("original")
    run = HandoffRun(session, "Build a portfolio")
    summary(run)
    run.finish("complete")
    assert "Portfolio accessibility" in handle_handoff_command(session)
    assert "Suggested Skills" in handle_handoff_command(session, f"preview {run.record['id']}")
    assert "Loaded handoff" in handle_handoff_command(session, f"resume {run.record['id']}")
    assert session.handoff_pending["id"] == run.record["id"]
    assert not session.auto_approve
    other = tmp_path / "other-project"
    other.mkdir()
    assert HandoffMemory(str(other)).get(run.record["id"]) is None
    assert "not found" in handle_handoff_command(state(other), f"resume {run.record['id']}")


def test_linked_worktrees_share_handoffs_and_warn_without_switching(tmp_path: Path):
    root = tmp_path / "repo"
    common = root / ".git"
    worktree = tmp_path / "linked"
    gitdir = common / "worktrees" / "linked"
    gitdir.mkdir(parents=True)
    (common / "HEAD").write_text("original")
    (gitdir / "HEAD").write_text("changed")
    (gitdir / "commondir").write_text("../..")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir.as_posix()}")
    run = HandoffRun(state(root), "Fix navigation")
    target = state(worktree)
    message = prepare(target, run.record["id"])
    assert "Workspace differs" in message
    assert "Revision changed" in message
    assert target.working_directory == str(worktree)


def test_changed_files_and_dependencies_are_warned(tmp_path: Path):
    (tmp_path / "app.py").write_text("original")
    session = state(tmp_path)
    run = HandoffRun(session, "Check app")
    summary(run)
    assert "changed or are unavailable" not in prepare(session, run.record["id"])
    (tmp_path / "package.json").write_text("{}")
    assert "changed or are unavailable" in prepare(session, run.record["id"])
    (tmp_path / "app.py").write_text("changed")
    assert "changed or are unavailable" in prepare(session, run.record["id"])


def test_restart_restores_selection_without_models_or_saved_approvals(tmp_path: Path):
    session = state(tmp_path)
    run = HandoffRun(session, "Build portfolio")
    summary(run)
    prepare(session, run.record["id"])
    payload = handoff_payload(session)
    recovered = state(tmp_path, "fresh", enabled=False)
    restore_handoff(recovered, payload)
    assert recovered.handoff_enabled
    assert recovered.handoff_pending["id"] == run.record["id"]
    assert not recovered.auto_approve
    child = HandoffRun(recovered, "Continue")
    assert child.record["parent_id"] == run.record["id"]
    assert child.record["thread"] == "fresh"
    assert HandoffMemory(str(tmp_path)).get(run.record["id"])["thread"] == "source"
    restore_handoff(recovered, None)
    assert not recovered.handoff_enabled
    assert recovered.handoff_pending is None


def test_reset_clears_context_but_keeps_explicit_toggle():
    session = SessionState()
    session.handoff_enabled = True
    session.handoff_pending = {"id": "old"}
    session.handoff_latest = {"id": "old"}
    session.reset_conversation()
    assert session.handoff_enabled
    assert session.handoff_pending is session.handoff_latest is None


def test_redaction_bounds_and_no_repository_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "example-secret-value-123456789")
    session = state(tmp_path)
    run = HandoffRun(session, "Use example-secret-value-123456789")
    summary(run)
    run.summarize(
        "Large summary",
        "objective",
        {
            "progress": ["x" * 5000] * 100,
            "decisions": ["password: not-for-storage"],
            "questions": ["$env:API_KEY=not-for-storage"],
        },
    )
    run.finish("complete")
    saved = HandoffMemory(str(tmp_path)).get(run.record["id"])
    assert "example-secret-value" not in str(saved)
    assert "not-for-storage" not in str(saved)
    assert len(saved["progress"]) == 12
    assert len(saved["progress"][0]) == 500
    assert not list(tmp_path.iterdir())


def test_late_and_subagent_calls_cannot_overwrite_the_current_session(tmp_path: Path):
    session = state(tmp_path)
    run = HandoffRun(session, "Old")
    assert run.owns({"configurable": {"thread_id": "source", "checkpoint_ns": "tools:node"}})
    assert not run.owns({"configurable": {"thread_id": "other"}})
    assert not run.owns(
        {"configurable": {"thread_id": "source", "checkpoint_ns": "tools:node|child"}}
    )
    newer = HandoffRun(session, "New")
    assert not run.owns({"configurable": {"thread_id": "source"}})
    run.finish("cancelled")
    assert session.handoff_latest is newer.record


def test_disabled_middleware_hides_tools_and_preserves_system_metadata():
    system = SystemMessage(content="Original", additional_kwargs={"cache": "keep"})
    request = ModelRequest(
        model=Model(responses=[AIMessage("ok")]),
        messages=[],
        system_message=system,
        tools=[handoff_save],
    )
    prepared = HandoffMiddleware.prepare(request)
    assert prepared.tools == []
    assert prepared.system_message is system
    assert request.tools == [handoff_save]


async def test_real_agent_saves_summary_and_fresh_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prepared_context = []
    original_prepare = HandoffMiddleware.prepare

    def capture(request: ModelRequest) -> ModelRequest:
        prepared = original_prepare(request)
        prepared_context.append(str(prepared.system_message.content))
        return prepared

    monkeypatch.setattr(HandoffMiddleware, "prepare", staticmethod(capture))
    arguments = {
        "title": "Portfolio",
        "objective": "Ship a portfolio",
        "progress": ["Navigation implemented"],
        "decisions": ["Astro"],
        "questions": ["Contact endpoint missing"],
        "next_steps": ["Run build"],
        "references": ["package.json"],
        "suggested_skills": ["frontend-design"],
    }
    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "handoff_save", "args": arguments, "id": "handoff-1"}],
            ),
            AIMessage(content="Navigation done; contact needs setup."),
        ]
    )
    graph = create_agent(model, middleware=[HandoffMiddleware()], checkpointer=InMemorySaver())
    session = state(tmp_path)
    events = [
        e async for e in iterate_agent_events("Build portfolio", graph, "nova-agent", session)
    ]
    assert not [e for e in events if isinstance(e, ev.Error)]
    record = HandoffMemory(str(tmp_path)).records()[0]
    assert record["title"] == "Portfolio"
    assert record["summary_kind"].startswith("agent summary")
    assert record["next_steps"] == ["Run build"]
    assert record["status"] == "saved"
    assert "contact needs setup" in record["last_response"]
    assert isinstance(events[-1], ev.Done)
    assert current_handoff.get() is None

    fresh = state(tmp_path, "fresh")
    assert "Loaded handoff" in prepare(fresh, record["id"])
    old = deepcopy(record)
    continued = [e async for e in iterate_agent_events("Continue", graph, "nova-agent", fresh)]
    assert not [e for e in continued if isinstance(e, ev.Error)]
    assert fresh.handoff_latest["parent_id"] == record["id"]
    assert HandoffMemory(str(tmp_path)).get(record["id"]) == old
    snapshot = await graph.aget_state({"configurable": {"thread_id": "fresh"}})
    assert snapshot.values["messages"][0].content == "Continue"
    assert len([m for m in snapshot.values["messages"] if m.type == "human"]) == 1
    assert "Contact endpoint missing" in prepared_context[-1]
    assert "Run build" in prepared_context[-1]
    assert "untrusted" in prepared_context[-1]


@pytest.mark.parametrize("enabled", [False, True])
async def test_model_omitting_summary_gets_honest_fallback(tmp_path: Path, *, enabled: bool):
    graph = create_agent(
        Model(responses=[AIMessage("Not tested yet")]),
        middleware=[HandoffMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = state(tmp_path, enabled=enabled)
    events = [e async for e in iterate_agent_events("Fix CSS", graph, "nova-agent", session)]
    assert not [e for e in events if isinstance(e, ev.Error)]
    records = HandoffMemory(str(tmp_path)).records()
    assert len(records) == int(enabled)
    if enabled:
        assert "summary not yet recorded" in document(records[0])
        assert "Not tested yet" in document(records[0])


@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_failed_runs_are_saved_as_paused(tmp_path: Path, failure: str):
    class Agent:
        async def aget_state(self, _config: object) -> SimpleNamespace:
            return SimpleNamespace(values={"messages": []})

        async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
            if failure == "cancelled":
                raise asyncio.CancelledError
            message = "offline failure"
            raise ValueError(message)
            yield

        aupdate_state = Mock()

    session = state(tmp_path)
    events = [e async for e in iterate_agent_events("Fix CSS", Agent(), "nova-agent", session)]
    assert isinstance(events[-1], ev.Cancelled if failure == "cancelled" else ev.Error)
    assert HandoffMemory(str(tmp_path)).records()[0]["status"] == "paused"
    assert current_handoff.get() is None


async def test_storage_failure_does_not_break_foreground_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def unavailable() -> None:
        message = "do not expose this storage detail"
        raise OSError(message)

    monkeypatch.setattr("novacode_cli.handoff.get_durable_store", unavailable)
    graph = create_agent(
        Model(responses=[AIMessage("Answer")]),
        middleware=[HandoffMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = state(tmp_path)
    events = [e async for e in iterate_agent_events("Task", graph, "nova-agent", session)]
    assert not [e for e in events if isinstance(e, ev.Error)]
    assert session.handoff_latest["last_response"] == "Answer"
    assert any(
        isinstance(e, ev.ContextMessage) and "storage unavailable" in e.message for e in events
    )
    assert "do not expose" not in str(events)


def test_changing_project_does_not_carry_another_projects_handoff(tmp_path: Path):
    session = state(tmp_path)
    original = HandoffRun(session, "Private project objective")
    other = tmp_path / "other"
    other.mkdir()
    session.working_directory = str(other)
    newer = HandoffRun(session, "Other project objective")
    assert newer.source is None
    assert newer.record["parent_id"] is None
    assert original.record["objective"] not in str(newer.record)


def test_sqlite_and_session_metadata_survive_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import sqlite3

    from novacode_cli.memory.store import _StdlibSqliteStore
    from novacode_cli.session.session_persistence import SessionManager

    connection = sqlite3.connect(tmp_path / "handoffs.db", isolation_level=None)
    store = _StdlibSqliteStore(connection)
    monkeypatch.setattr("novacode_cli.handoff.get_durable_store", lambda: store)
    session = state(tmp_path)
    run = HandoffRun(session, "Build portfolio")
    summary(run)
    run.finish("cancelled")
    prepare(session, run.record["id"])
    manager = SessionManager(tmp_path / "sessions")
    manager.save_session(
        "session",
        "source",
        [HumanMessage("Build portfolio")],
        "nova-agent",
        handoff=handoff_payload(session),
    )
    connection.close()

    connection = sqlite3.connect(tmp_path / "handoffs.db", isolation_level=None)
    store = _StdlibSqliteStore(connection)
    recovered = state(tmp_path, "fresh", enabled=False)
    saved = SessionManager(tmp_path / "sessions").load_session("session")
    restore_handoff(recovered, saved.meta.handoff)
    assert recovered.handoff_enabled
    assert recovered.handoff_pending["id"] == run.record["id"]
    assert recovered.handoff_pending["next_steps"] == ["Run keyboard checks"]
    assert not recovered.auto_approve
    connection.close()


async def test_concurrent_continuations_keep_separate_children(tmp_path: Path):
    run = HandoffRun(state(tmp_path), "Build portfolio")
    summary(run)
    original = HandoffMemory(str(tmp_path)).get(run.record["id"])

    async def continue_task(name: str) -> SimpleNamespace:
        session = state(tmp_path, name)
        prepare(session, run.record["id"])
        graph = create_agent(
            Model(responses=[AIMessage("Next step")]),
            middleware=[HandoffMiddleware()],
            checkpointer=InMemorySaver(),
        )
        events = [e async for e in iterate_agent_events("Continue", graph, "nova-agent", session)]
        assert not any(isinstance(e, ev.Error) for e in events)
        return session

    first, second = await asyncio.gather(continue_task("first"), continue_task("second"))
    assert first.handoff_latest["id"] != second.handoff_latest["id"]
    assert (
        first.handoff_latest["parent_id"] == second.handoff_latest["parent_id"] == run.record["id"]
    )
    assert HandoffMemory(str(tmp_path)).get(run.record["id"]) == original
    assert current_handoff.get() is None


async def test_prime_and_handoff_can_share_the_foreground_loop(tmp_path: Path):
    from novacode_cli.agents.prime import PrimeMiddleware
    from novacode_cli.prime import current_run

    session = state(tmp_path)
    session.prime_enabled = True
    session.prime_report = None
    graph = create_agent(
        Model(responses=[AIMessage("Unverified answer")]),
        middleware=[PrimeMiddleware(), HandoffMiddleware()],
        checkpointer=InMemorySaver(),
    )
    events = [
        e async for e in iterate_agent_events("Review portfolio", graph, "nova-agent", session)
    ]
    assert not [e for e in events if isinstance(e, ev.Error)]
    assert session.handoff_latest["status"] == "saved"
    assert session.prime_report["status"] == "incomplete"
    assert current_handoff.get() is current_run.get() is None
