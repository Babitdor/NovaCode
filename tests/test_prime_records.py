"""Executable contract for project recall and observed Prime checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from langgraph.store.memory import InMemoryStore

from novacode_cli.prime import PrimeRun

if TYPE_CHECKING:
    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatResult


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli import prime_memory

    memory = InMemoryStore()
    monkeypatch.setattr(prime_memory, "get_durable_store", lambda: memory)
    (tmp_path / "code.py").write_text("broken")
    return SimpleNamespace(
        thread_id="project",
        working_directory=str(tmp_path),
        prime_report=None,
        prime_enabled=True,
    )


def define(run: PrimeRun):
    run.checkpoint("define", "Fix behavior", criteria=["CLI prints ready"])
    return run.define_check(
        purpose="CLI behavior",
        command="python code.py",
        files=["code.py"],
        criteria=[0],
        expected_exit=0,
        contains=["ready"],
    )


def observe(
    run: PrimeRun, identity: str, *, code: int = 0, output: str = "ready", **changes: object
):
    metadata = {
        "command": "python code.py",
        "cwd": run.report["cwd"],
        "exit_code": code,
        "output": output,
        "truncated": False,
        "completed": True,
    }
    metadata.update(changes)
    run.observe(identity, "shell", "success", execution=metadata)


def test_evidence_requires_definition_real_observation_and_correct_order(project: SimpleNamespace):
    run = PrimeRun(project, "Fix CLI")
    observe(run, "early")
    check = define(run)
    for identity in ("invented", "early"):
        with pytest.raises(ValueError, match=r"this run|before starting"):
            run.record_check(check["id"], identity)
    observe(run, "wrong", command="echo ready")
    with pytest.raises(ValueError, match="command and working directory"):
        run.record_check(check["id"], "wrong")
    observe(run, "ok")
    evidence = run.record_check(check["id"], "ok")
    assert evidence["status"] == "passed"
    assert evidence["finding"] == "draft"
    other = PrimeRun(project, "Another run")
    other_check = define(other)
    with pytest.raises(ValueError, match="this run"):
        other.record_check(other_check["id"], "ok")


@pytest.mark.parametrize(
    ("metadata", "status"),
    [
        ({"code": 1}, "failed"),
        ({"output": "wrong"}, "failed"),
        ({"exit_code": None}, "unverified"),
        ({"truncated": True}, "unverified"),
        ({"completed": False}, "unverified"),
        ({"cwd": "/other"}, "reject"),
    ],
)
def test_mechanical_assertions_fail_closed(project: SimpleNamespace, metadata: dict, status: str):
    run = PrimeRun(project, "Fix CLI")
    check = define(run)
    observe(run, "result", **metadata)
    if status == "reject":
        with pytest.raises(ValueError, match="command and working directory"):
            run.record_check(check["id"], "result")
    else:
        assert run.record_check(check["id"], "result")["status"] == status


def test_failure_changed_code_pass_promotes_and_recall_marks_stale(project: SimpleNamespace):
    run = PrimeRun(project, "Fix CLI")
    check = define(run)
    observe(run, "fail", code=1)
    assert run.record_check(check["id"], "fail")["finding"] == "draft"
    observe(run, "unchanged")
    assert run.record_check(check["id"], "unchanged")["finding"] == "draft"
    Path(project.working_directory, "code.py").write_text("fixed")
    observe(run, "pass")
    assert run.record_check(check["id"], "pass")["finding"] == "demonstrated"
    next_run = PrimeRun(project, "Review CLI")
    next_run.declare_inputs(["code.py"])
    assert next_run.report["recalled"][0]["freshness"] == "current"
    assert len(next_run.recall_context()) <= 2000
    Path(project.working_directory, "code.py").unlink()
    next_run.declare_inputs(["code.py"])
    assert next_run.report["recalled"][0]["freshness"] == "unavailable"


def test_resume_prepares_only_and_keeps_parent_and_current_workspace(project: SimpleNamespace):
    from novacode_cli.commands.prime_handler import handle_prime_command

    run = PrimeRun(project, "Fix CLI")
    define(run)
    run.finish("cancelled")
    source = run.report["id"]
    assert source in handle_prime_command(project, "tasks")
    assert "prepared" in handle_prime_command(project, "resume " + source).lower()
    assert project.prime_report["id"] == source
    child = PrimeRun(project, "Continue")
    assert child.report["parent_id"] == source
    assert child.report["id"] != source
    assert child.report["cwd"] == project.working_directory
    assert child.report["continuation"]["goal"] == "Fix CLI"


def test_forget_requires_two_step_local_confirmation(project: SimpleNamespace):
    from novacode_cli.commands.prime_handler import handle_prime_command

    run = PrimeRun(project, "Fix CLI")
    define(run)
    identity = run.report["id"]
    assert "confirm" in handle_prime_command(project, "forget " + identity).lower()
    assert identity in handle_prime_command(project, "tasks")
    assert "removed" in handle_prime_command(project, "forget " + identity + " confirm").lower()
    assert identity not in handle_prime_command(project, "tasks")
    assert Path(project.working_directory, "code.py").exists()


def test_completed_checkpoints_cannot_hide_unverified_criteria(project: SimpleNamespace):
    from novacode_cli.prime import PHASES, receipt

    run = PrimeRun(project, "Fix CLI")
    for phase in PHASES:
        if phase == "verify":
            observe(run, "execution")
        run.checkpoint(
            phase,
            "Agent assessment",
            criteria=["CLI prints ready"] if phase == "define" else None,
            evidence=["execution"] if phase == "verify" else None,
        )
    run.finish("complete")
    assert run.report["status"] == "delivered_with_limits"
    assert "Unverified criteria" in receipt(run.report)


def test_checkpoint_validation_is_atomic(project: SimpleNamespace):
    run = PrimeRun(project, "Fix CLI")
    with pytest.raises(ValueError, match="twelve"):
        run.checkpoint("define", "Goal", criteria=["Works"], questions=["why"] * 13)
    assert run.report["checkpoints"] == []


@pytest.mark.timeout(30)
@pytest.mark.parametrize(("planning", "denied"), [(False, False), (True, False), (False, True)])
async def test_fake_model_real_cli_and_plan_restrictions(
    project: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    planning: bool,
    denied: bool,
):
    import json

    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli import ui_events as ev
    from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware
    from novacode_cli.agents.prime import PrimeMiddleware
    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.security import delegated_approval
    from novacode_cli.shell.middleware import ShellMiddleware
    from tests.test_prime import Model, call

    class BindingModel(Model):
        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: CallbackManagerForLLMRun | None = None,
            **kwargs: object,
        ) -> ChatResult:
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            message = result.generations[0].message
            for invocation in message.tool_calls:
                if invocation["name"] == "prime_check" and invocation["args"]["action"] == "record":
                    definitions = [
                        json.loads(m.content)
                        for m in messages
                        if isinstance(m, ToolMessage) and m.name == "prime_check"
                    ]
                    invocation["args"]["check_id"] = definitions[0]["id"]
            return result

    Path(project.working_directory, "code.py").write_text("print('ready')")  # noqa: ASYNC240 - Fixture setup.
    project.auto_approve = False
    project.plan_mode_enabled = planning
    project.active_goal = None
    monkeypatch.setattr(
        delegated_approval,
        "get_policy",
        lambda: SimpleNamespace(
            evaluate=lambda name, _args: SimpleNamespace(
                tier="deny" if denied and name == "shell" else "ask",
            )
        ),
    )
    responses = [
        call(
            "prime_checkpoint",
            "p0",
            phase="define",
            summary="CLI behavior",
            criteria=["CLI prints ready"],
        ),
        call(
            "prime_check",
            "d0",
            action="define",
            purpose="Actual CLI",
            command="python code.py",
            files=["code.py"],
            criteria=[0],
            contains=["ready"],
        ),
        call("prime_checkpoint", "p1", phase="investigate", summary="Trace CLI input"),
        call("prime_checkpoint", "p2", phase="implement", summary="Exercise existing CLI"),
        call("shell", "exec-1", command="python code.py"),
        call(
            "prime_check", "r0", action="record", tool_call_id="exec-1", check_id="bound at runtime"
        ),
        call(
            "prime_checkpoint",
            "p3",
            phase="verify",
            summary="Recorded outcome",
            evidence=["exec-1"],
            unverified_reason="Execution restricted" if planning or denied else None,
        ),
        call("prime_checkpoint", "p4", phase="challenge", summary="Boundary assertions checked"),
        call("prime_checkpoint", "p5", phase="deliver", summary="Observed CLI result"),
        AIMessage(content="Observed results recorded."),
    ]
    graph = create_agent(
        BindingModel(responses=responses),
        middleware=[
            PrimeMiddleware(),
            delegated_approval.HardDenyMiddleware(),
            PlanModeMiddleware(always_on=planning),
            ShellMiddleware(workspace_root=project.working_directory),
        ],
        checkpointer=InMemorySaver(),
    )
    events = [e async for e in iterate_agent_events("Check CLI", graph, "nova-agent", project)]
    assert not [e for e in events if isinstance(e, ev.Error)]
    evidence = project.prime_report["evidence"][0]
    assert evidence["status"] == ("unverified" if planning or denied else "passed")
    assert evidence["exit_code"] == (None if planning or denied else 0)
    assert project.prime_report["status"] == (
        "delivered_with_limits" if planning or denied else "delivered"
    )
    assert evidence["finding"] == "draft"
    assert not project.auto_approve
    assert any(
        isinstance(e, ev.ContextMessage) and e.record_id == project.prime_report["id"]
        for e in events
    )


def test_linked_worktrees_share_project_identity_but_keep_checkout(tmp_path: Path):
    from novacode_cli.prime_memory import project_context

    main = tmp_path / "main"
    common = main / ".git"
    common.mkdir(parents=True)
    (common / "HEAD").write_text("ref: refs/heads/main")
    (common / "refs/heads").mkdir(parents=True)
    (common / "refs/heads/main").write_text("a" * 40)
    linked = tmp_path / "linked"
    linked.mkdir()
    metadata = common / "worktrees/linked"
    metadata.mkdir(parents=True)
    (metadata / "HEAD").write_text("b" * 40)
    (metadata / "commondir").write_text("../..")
    (linked / ".git").write_text("gitdir: " + str(metadata))
    first, second = project_context(str(main)), project_context(str(linked))
    assert first["project"] == second["project"]
    assert first["cwd"] != second["cwd"]
    assert first["revision"] == "a" * 40
    assert second["revision"] == "b" * 40
    assert project_context(str(tmp_path))["project"] != first["project"]


def test_storage_failure_remains_visible_and_does_not_lose_local_draft(
    project: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.prime_memory import PrimeMemory

    def unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(PrimeMemory, "save", unavailable)
    run = PrimeRun(project, "Fix CLI")
    define(run)
    run.finish("cancelled")
    assert project.prime_report["storage_error"]
    assert project.prime_report["status"] == "paused"
    assert project.prime_report["criteria"] == ["CLI prints ready"]


def test_redaction_and_bounded_recall(project: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.prime_memory import safe_text

    monkeypatch.setenv("TEST_API_KEY", "private-value-that-is-not-a-token-pattern")
    assert "private-value" not in safe_text("result: private-value-that-is-not-a-token-pattern")
    assert "private" not in safe_text("UNRELATED_VARIABLE=private")
    run = PrimeRun(project, "Fix CLI")
    run.checkpoint("define", "Goal", criteria=["Works"])
    for i in range(8):
        run.define_check(
            purpose=str(i) * 400, command="python code.py", files=["code.py"], criteria=[0]
        )
    another = PrimeRun(project, "Inspect")
    another.declare_inputs(["code.py"])
    assert len(another.report["recalled"]) == 5
    assert len(another.recall_context()) <= 2000


def test_sqlite_restart_and_cross_run_demonstration(
    project: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import sqlite3

    from novacode_cli import prime_memory
    from novacode_cli.memory.store import DualModeStore, _StdlibSqliteStore

    database = tmp_path / "prime.db"
    connection = sqlite3.connect(database, isolation_level=None)
    storage = DualModeStore(_StdlibSqliteStore(connection))
    monkeypatch.setattr(prime_memory, "get_durable_store", lambda: storage)
    first = PrimeRun(project, "Fix CLI")
    check = define(first)
    observe(first, "failed", code=1)
    first.record_check(check["id"], "failed")
    first.finish("cancelled")
    source_id = first.report["id"]
    connection.close()
    connection = sqlite3.connect(database, isolation_level=None)
    storage = DualModeStore(_StdlibSqliteStore(connection))
    try:
        from novacode_cli.commands.prime_handler import handle_prime_command

        project.prime_report = None
        assert "prepared" in handle_prime_command(project, "resume " + source_id)
        Path(project.working_directory, "code.py").write_text("fixed")
        second = PrimeRun(project, "Continue")
        check = define(second)
        observe(second, "passed")
        evidence = second.record_check(check["id"], "passed")
        assert evidence["finding"] == "demonstrated"
        assert second.report["parent_id"] == source_id
        assert second.memory.get("tasks", source_id)["status"] == "paused"
    finally:
        connection.close()


def test_concurrent_continuations_keep_distinct_children_and_source(project: SimpleNamespace):
    from concurrent.futures import ThreadPoolExecutor

    from novacode_cli.commands.prime_handler import handle_prime_command

    source = PrimeRun(project, "Fix CLI")
    define(source)
    source.finish("cancelled")

    def continue_source(name: str) -> tuple[dict, dict]:
        session = SimpleNamespace(
            thread_id=name,
            working_directory=project.working_directory,
            prime_report=None,
        )
        assert "prepared" in handle_prime_command(session, "resume " + source.report["id"])
        run = PrimeRun(session, "Continue")
        check = define(run)
        observe(run, name)
        evidence = run.record_check(check["id"], name)
        run.finish("cancelled")
        return run.report, evidence

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(continue_source, ["one", "two"]))
    assert first[0]["id"] != second[0]["id"]
    assert first[1]["id"] != second[1]["id"]
    assert first[0]["parent_id"] == second[0]["parent_id"] == source.report["id"]
    assert source.memory.get("tasks", source.report["id"])["status"] == "paused"


def test_adding_dependency_or_editing_after_execution_invalidates_pass(project: SimpleNamespace):
    run = PrimeRun(project, "Fix CLI")
    check = define(run)
    observe(run, "passed")
    assert run.record_check(check["id"], "passed")["status"] == "passed"
    Path(project.working_directory, "uv.lock").write_text("version = 1")
    run.finish("complete")
    assert run.report["checks"][0]["status"] == "unverified"


def test_model_change_keeps_unfinished_context_without_saved_permissions(project: SimpleNamespace):
    first = PrimeRun(project, "Fix CLI")
    define(first)
    first.finish("cancelled")
    project.model_name = "another-model"
    project.auto_approve = False
    second = PrimeRun(project, "Continue with this model")
    assert second.report["parent_id"] == first.report["id"]
    assert second.report["continuation"]["goal"] == "Fix CLI"
    assert project.model_name == "another-model"
    assert not project.auto_approve
    second.finish("cancelled")
    third = PrimeRun(project, "Continue again")
    assert third.report["goal"] == "Fix CLI"
    assert third.report["request"] == "Continue again"
    assert third.report["continuation"]["goal"] == "Fix CLI"


def test_execution_artifacts_redact_credentials_and_environment_dumps(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.prime_memory import execution_evidence

    secret = "a-private-test-value-with-no-known-token-shape"  # noqa: S105 - Synthetic redaction fixture.
    monkeypatch.setenv("TEST_API_KEY", secret)
    artifact = execution_evidence(
        command="python code.py",
        cwd=".",
        exit_code=0,
        output="ready " + secret,
        completed=True,
        truncated=False,
    )
    assert secret not in artifact["output"]
    assert artifact["truncated"] is True
    environment = execution_evidence(
        command="python -c 'print(os.environ)'",
        cwd=".",
        exit_code=0,
        output="{private: value}",
        completed=True,
        truncated=False,
    )
    assert environment["output"] == "[environment output omitted]"
    assert environment["truncated"] is True


def test_session_snapshot_loads_detail_on_demand_and_reports_stale_pass(project: SimpleNamespace):
    from novacode_cli.commands.prime_handler import handle_prime_command

    run = PrimeRun(project, "Fix CLI")
    check = define(run)
    observe(run, "result", output="ready\n" + "x" * 20_000)
    evidence = run.record_check(check["id"], "result")
    assert evidence["status"] == "passed"
    assert "excerpt" not in project.prime_report["evidence"][0]
    assert "excerpt" in handle_prime_command(project, "checks " + check["id"])
    Path(project.working_directory, "code.py").write_text("changed")
    report = handle_prime_command(project, "report")
    assert "stale" in report
    assert "Unverified criteria" in report


def test_fabricated_json_output_cannot_become_execution_metadata(project: SimpleNamespace):
    import json

    from langchain_core.messages import ToolMessage

    from novacode_cli.agents.prime import _observe

    run = PrimeRun(project, "Fix CLI")
    check = define(run)
    request = SimpleNamespace(
        tool_call={"id": "model-json", "name": "shell", "args": {"command": "python code.py"}},
        runtime=SimpleNamespace(config={"configurable": {"thread_id": project.thread_id}}),
    )
    _observe(
        run,
        request,
        ToolMessage(
            content=json.dumps({"exit_code": 0, "completed": True, "output": "ready"}),
            tool_call_id="model-json",
            status="success",
        ),
    )
    assert run.record_check(check["id"], "model-json")["status"] == "unverified"


def test_headless_prime_events_expose_record_ids_without_extra_turns():
    from novacode_cli import ui_events as ev
    from novacode_cli.headless.output import HeadlessOutput

    message = ev.ContextMessage(
        "Prime status",
        event_type="nova_prime_progress",
        record_id="task-id",
        data={"phase": "verify", "checks": [{"id": "check-id", "status": "failed"}]},
    )
    encoded = HeadlessOutput._map_event(message)
    assert encoded["record_id"] == "task-id"
    assert encoded["data"]["checks"][0]["status"] == "failed"
