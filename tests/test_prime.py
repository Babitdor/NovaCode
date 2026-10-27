"""Prime Deep Work is opt-in, observable, session-scoped and permission-neutral."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli import ui_events as ev
from novacode_cli.agents.prime import INSTRUCTIONS, PrimeMiddleware, prime_checkpoint
from novacode_cli.commands.prime_handler import handle_prime_command
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.prime import PHASES, PrimeRun, current_run, prime_payload, receipt, restore_prime
from novacode_cli.states.Session import SessionState

if TYPE_CHECKING:
    from pathlib import Path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class Model(FakeMessagesListChatModel):
    def bind_tools(self, _tools: list, **_kwargs: object) -> Model:
        return self


def state(name: str = "prime", *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        thread_id=name,
        prime_enabled=enabled,
        prime_report=None,
        auto_approve=False,
        plan_mode_enabled=False,
        active_goal=None,
    )


def test_commands_never_start_work_or_change_permissions():
    session = state(enabled=False)
    original = vars(session).copy()
    assert "off" in handle_prime_command(session, "status")
    assert vars(session) == original
    assert "on" in handle_prime_command(session, "on")
    assert session.prime_enabled
    assert not session.auto_approve
    assert not session.plan_mode_enabled
    assert "Usage" in handle_prime_command(session, "invalid")
    assert session.prime_enabled
    assert "No Prime delivery" in handle_prime_command(session, "report")
    assert "off" in handle_prime_command(session, "off")
    assert not session.prime_enabled


def test_ordered_checkpoints_and_observed_execution_evidence():
    session = state()
    run = PrimeRun(session, "Fix login")
    with pytest.raises(ValueError, match="in order"):
        run.checkpoint("deliver", "Done")
    with pytest.raises(ValueError, match="criteria"):
        run.checkpoint("define", "Define success")
    run.checkpoint("define", "Reproduce and fix", criteria=["Expired cookie is cleared"])
    with pytest.raises(ValueError, match="owner decision"):
        run.checkpoint("investigate", "Explore", criteria=["Rewrite authentication"])
    with pytest.raises(ValueError, match="observed tool-call"):
        run.checkpoint("investigate", "Found cause", evidence=["invented"])
    run.observe("read-1", "read_file", "success")
    run.checkpoint("investigate", "Read login handler", evidence=["read-1"])
    run.checkpoint("implement", "Clear the expired cookie")
    with pytest.raises(ValueError, match="execution evidence"):
        run.checkpoint("verify", "Tests pass", evidence=["read-1"])
    run.observe("test-1", "shell", "error")
    with pytest.raises(ValueError, match="execution evidence"):
        run.checkpoint("verify", "Tests pass", evidence=["test-1"])
    run.observe("test-2", "shell", "success")
    run.checkpoint("verify", "Focused regression passes", evidence=["test-2"])
    run.checkpoint("challenge", "Repeated login and missing cookies covered", evidence=["test-2"])
    run.checkpoint("deliver", "Change ready for owner review")
    run.finish("complete")
    assert session.prime_report["status"] == "delivered"
    assert len(session.prime_report["checkpoints"]) == 6
    assert "agent assessment" in receipt(session.prime_report)
    assert "does not by itself prove tests passed" in receipt(session.prime_report)
    with pytest.raises(ValueError, match="immutable"):
        run.checkpoint("deliver", "Repeated")


def test_unverified_and_missing_checks_cannot_be_certified():
    session = state()
    run = PrimeRun(session, "Review a proposal")
    for phase in PHASES:
        run.checkpoint(
            phase,
            "Read-only assessment",
            criteria=["Explain tradeoffs"] if phase == "define" else None,
            unverified_reason="No executable changes; runtime checks do not apply"
            if phase == "verify"
            else None,
        )
    run.finish("complete")
    assert session.prime_report["status"] == "delivered_with_limits"
    assert "Not verified" in receipt(session.prime_report)
    run = PrimeRun(session, "Another request")
    run.finish("complete")
    assert session.prime_report["status"] == "incomplete"
    assert "Missing checkpoints" in receipt(session.prime_report)


def test_session_snapshots_restore_without_replaying_and_clear_drops_evidence():
    session = SessionState()
    session.prime_enabled = True
    run = PrimeRun(session, "Fix a bug")
    run.checkpoint("define", "Reproduce it", criteria=["Regression passes"])
    saved = prime_payload(session)
    assert saved["report"]["status"] == "running"
    run.observe("later", "read_file", "success")
    assert saved["report"]["tools"] == []
    other = state(session.thread_id)
    restore_prime(other, saved)
    assert other.prime_enabled
    assert other.prime_report["status"] == "paused"
    assert not other.auto_approve
    restore_prime(other, None)
    assert not other.prime_enabled
    assert other.prime_report is None
    restore_prime(state("different"), saved)
    assert prime_payload(state("different"))["report"] is None
    session.reset_conversation()
    assert session.prime_enabled
    assert session.prime_report is None


def test_late_results_from_a_cleared_request_cannot_restore_old_progress():
    session = SessionState()
    session.prime_enabled = True
    run = PrimeRun(session, "Old task")
    old_thread = session.thread_id
    session.reset_conversation()
    run.observe("late", "shell", "success")
    run.finish("complete")
    assert session.prime_report is None
    assert not run.owns({"configurable": {"thread_id": old_thread}})


def test_late_results_cannot_overwrite_a_newer_request_in_the_same_thread():
    session = state()
    old = PrimeRun(session, "Old task")
    new = PrimeRun(session, "New task")
    old.observe("late", "shell", "success")
    old.finish("complete")
    assert session.prime_report["id"] == new.report["id"]
    assert session.prime_report["goal"] == "New task"
    assert not old.owns({"configurable": {"thread_id": session.thread_id}})
    assert new.owns({"configurable": {"thread_id": session.thread_id}})


def test_request_copy_preserves_history_model_and_system_cache_metadata():
    model = Model(responses=[AIMessage("ok")])
    system = SystemMessage(
        content=[{"type": "text", "text": "Core policy", "cache_control": {"type": "ephemeral"}}]
    )
    request = ModelRequest(
        model=model,
        messages=[HumanMessage("Fix login")],
        system_message=system,
        tools=[prime_checkpoint],
    )
    before = deepcopy(system)
    disabled = PrimeMiddleware.prepare(request, None)
    assert disabled.tools == []
    assert disabled.messages == request.messages
    assert disabled.model is model
    prepared = PrimeMiddleware.prepare(request, PrimeRun(state(), "Fix login"))
    assert prepared.system_message.content[0] == system.content[0]
    assert INSTRUCTIONS in prepared.system_message.text
    assert system == before
    assert prepared.messages == request.messages
    assert prepared.model is model
    run = PrimeRun(state(), "Fix login")
    run.observe("sdk-generated-call-id", "shell", "success")
    assert "sdk-generated-call-id" in PrimeMiddleware.prepare(request, run).system_message.text


def test_root_binding_rejects_other_threads_and_subgraphs():
    run = PrimeRun(state(), "Fix login")
    assert run.owns({"configurable": {"thread_id": "prime", "checkpoint_ns": "tools:root"}})
    assert not run.owns({"configurable": {"thread_id": "other"}})
    assert not run.owns(
        {"configurable": {"thread_id": "prime", "checkpoint_ns": "tools:root|agent:child"}}
    )


def call(name: str, identity: str, **args: object) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": identity, "args": args}])


@pytest.mark.timeout(30)
async def test_real_graph_records_phases_results_and_host_delivery_receipt():
    @tool
    async def run_tests() -> str:
        """Run a deterministic offline check."""
        return "1 passed"

    responses = [
        call(
            "prime_checkpoint",
            "p0",
            phase="define",
            summary="Define success",
            criteria=["Focused check passes"],
        ),
        call("prime_checkpoint", "p1", phase="investigate", summary="Trace code and failure"),
        call("prime_checkpoint", "p2", phase="implement", summary="Implement focused fix"),
        call("run_tests", "tests-1"),
        call(
            "prime_checkpoint", "p3", phase="verify", summary="Check passed", evidence=["tests-1"]
        ),
        call(
            "prime_checkpoint",
            "p4",
            phase="challenge",
            summary="Boundary input covered",
            evidence=["tests-1"],
        ),
        call("prime_checkpoint", "p5", phase="deliver", summary="Ready for owner review"),
        AIMessage(content="Change ready."),
    ]
    graph = create_agent(
        Model(responses=responses),
        tools=[run_tests],
        middleware=[PrimeMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = state()
    events = [e async for e in iterate_agent_events("Fix login", graph, "nova-agent", session)]
    assert not [e for e in events if isinstance(e, ev.Error)]
    assert session.prime_report["status"] == "delivered", session.prime_report
    assert [r["phase"] for r in session.prime_report["checkpoints"]] == list(PHASES)
    assert session.prime_report["tools"][0]["id"] == "tests-1"
    assert any(
        isinstance(e, ev.ContextMessage) and e.event_type == "nova_prime_progress" for e in events
    )
    assert any(
        isinstance(e, ev.ContextMessage) and e.event_type == "nova_prime_receipt" for e in events
    )
    assert isinstance(events[-1], ev.Done)
    assert current_run.get() is None
    assert session.auto_approve is False


async def test_parallel_runs_and_disabled_requests_do_not_leak_context():
    async def execute(name: str, *, enabled: bool) -> SimpleNamespace:
        graph = create_agent(
            Model(responses=[AIMessage(content="Answer")]),
            middleware=[PrimeMiddleware()],
            checkpointer=InMemorySaver(),
        )
        session = state(name, enabled=enabled)
        events = [e async for e in iterate_agent_events(name, graph, "nova-agent", session)]
        assert not [e for e in events if isinstance(e, ev.Error)]
        return session

    first, second = await asyncio.gather(
        execute("first", enabled=True), execute("second", enabled=False)
    )
    assert first.prime_report["thread"] == "first"
    assert first.prime_report["status"] == "incomplete"
    assert second.prime_report is None
    assert current_run.get() is None


@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_errors_and_cancellation_pause_or_fail_prime_without_permission_changes(failure: str):
    class Agent:
        async def aget_state(self, _config: dict) -> SimpleNamespace:
            return SimpleNamespace(values={"messages": []})

        async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
            if failure == "cancelled":
                raise asyncio.CancelledError
            msg = "offline failure"
            raise ValueError(msg)
            yield

        aupdate_state = Mock()

    session = state()
    events = [e async for e in iterate_agent_events("Fix login", Agent(), "nova-agent", session)]
    assert isinstance(events[-1], ev.Cancelled if failure == "cancelled" else ev.Error)
    assert session.prime_report["status"] == ("paused" if failure == "cancelled" else "error")
    assert not session.auto_approve
    assert current_run.get() is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": {}},
        {"status": "delivered"},
        {"phase": "grant_permissions"},
        {"tools": None},
        {"tools": [{"id": "x", "name": "shell", "status": []}]},
        {"criteria": [None]},
        {"checkpoints": [None]},
    ],
)
def test_malformed_saved_reports_do_not_restore_or_grant_permissions(change: dict):
    session = state()
    report = deepcopy(PrimeRun(session, "Fix login").report)
    report.update(change)
    restored = state()
    restore_prime(restored, {"enabled": True, "report": report})
    assert restored.prime_report is None
    assert not restored.auto_approve


def test_session_manager_round_trip_and_legacy_metadata(tmp_path: Path):
    from novacode_cli.session.session_persistence import SessionManager, SessionMeta

    session = state()
    PrimeRun(session, "Fix login").checkpoint(
        "define", "Define success", criteria=["Regression passes"]
    )
    manager = SessionManager(tmp_path / "sessions")
    manager.save_session(
        "saved",
        session.thread_id,
        [HumanMessage("Fix login")],
        "nova-agent",
        project_root=tmp_path,
        prime=prime_payload(session),
    )
    loaded = manager.load_session("saved")
    restored = state()
    restore_prime(restored, loaded.meta.prime)
    assert restored.prime_enabled
    assert restored.prime_report["criteria"] == ["Regression passes"]
    assert restored.prime_report["status"] == "paused"
    old = loaded.meta.to_dict()
    del old["prime"]
    assert SessionMeta.from_dict(old).prime is None


@pytest.mark.parametrize(
    ("args", "expected"), [([], None), (["--prime"], True), (["--no-prime"], False)]
)
def test_cli_mode_flags_are_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch, args: list[str], *, expected: bool | None
):
    from novacode_cli.cli_args import parse_args

    monkeypatch.setattr("sys.argv", ["nova", *args])
    assert parse_args().prime is expected


async def test_host_receipt_does_not_consume_a_headless_model_turn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    import json

    from novacode_cli.headless.runner import EXIT_OK, run_headless
    from tests.test_headless import _fake_source, _headless_state

    events = [
        ev.AssistantMessage("Answer", "Nova", "cyan"),
        ev.ContextMessage("Prime receipt", event_type="nova_prime_receipt"),
        ev.Done(),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))
    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json", max_turns=1),
    )
    result = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert result["num_turns"] == 1
    assert "Prime receipt" in result["result"]
