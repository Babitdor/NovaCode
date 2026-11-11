"""Offline goal-to-delivery execution against real isolated Git repositories."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.prime_factory import ROLES
from novacode_cli.prime_factory.controller import FactoryController
from novacode_cli.prime_factory.runtime import is_factory_request
from novacode_cli.prime_factory.store import FactoryError, FactoryStore
from novacode_cli.prime_factory.workflow import FactoryWorkflow
from novacode_cli.sessions import worktree


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "offline@example.invalid"],
        ["config", "user.name", "Offline"],
    ):
        assert worktree._git(args, cwd=root)[0] == 0
    (root / "answer.py").write_text("def answer():\n    return 0\n")
    (root / "independent.py").write_text("value = 0\n")
    assert worktree._git(["add", "."], cwd=root)[0] == 0
    assert worktree._git(["commit", "-m", "baseline"], cwd=root)[0] == 0
    monkeypatch.setattr(worktree, "worktrees_root", lambda: tmp_path / "workers")
    c = FactoryController.create(
        root,
        "Fix the answer",
        "thread",
        {role: {"provider": "offline", "model": "scripted"} for role in ROLES},
        store=FactoryStore(tmp_path / "factory.sqlite3"),
    )
    yield c
    c.store.close()


class ScriptedRunner:
    """Scripted role assertions; checks execute in real subprocesses and real worktrees."""

    def __init__(self, controller: FactoryController, *, repair: bool = False) -> None:
        self.c = controller
        self.calls = []
        self.commands = []
        self.repair = repair
        self.fail_repairs = 0
        self.active = 0
        self.peak = 0
        self.developers_started = asyncio.Event()

    async def authorize(self, _name: str, _args: dict[str, Any]):
        return True

    async def delivery_base(self, _cwd: Path, _destination: str, _base: str) -> str:
        return self.c.record["baseline"]

    async def run(
        self,
        role: str,
        task: str,
        cwd: Path,
        _context: dict[str, Any],
        *,
        files: list[str] | None = None,
    ):
        self.calls.append((role, task))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if role == "developer" and self.active == 1:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.developers_started.wait(), timeout=5)
            elif role == "developer":
                self.developers_started.set()
            await asyncio.sleep(0.01)
            if role == "requirements":
                return {
                    "criteria": ["The answer is 42"],
                    "dependencies": {},
                    "summary": "Fix answer",
                }
            if role == "research":
                return {"sources": [], "unnecessary": "Only repository arithmetic changes"}
            if role == "architecture":
                return {
                    "tasks": [
                        {
                            "id": "answer",
                            "description": "Return the correct answer",
                            "files": ["answer.py"],
                            "depends_on": [],
                        },
                        {
                            "id": "independent",
                            "description": "Update independent constant",
                            "files": ["independent.py"],
                            "depends_on": [],
                        },
                    ],
                    "decisions": ["Retain the public function"],
                    "setup": [],
                    "destination": {"repository": "offline/project", "base": "main"},
                }
            if role == "test_engineer" and files is None:
                return {
                    "test_files": ["check_answer.py"],
                    "checks": [
                        {
                            "argv": [sys.executable, "check_answer.py"],
                            "purpose": "Answer contract",
                            "criteria": [0],
                            "test_first": True,
                            "security": False,
                            "tasks": ["answer"],
                            "failure_contains": ["answer must be 42"],
                        }
                    ],
                }
            if files is not None:
                self.c.require_plan()
                if role == "test_engineer":
                    (cwd / "check_answer.py").write_text(
                        "from answer import answer\nassert answer() == 42, 'answer must be 42'\n"
                    )
                elif task.startswith("answer"):
                    incorrect = self.repair and (
                        "repair" not in task or int(task.rsplit("-", 1)[1]) <= self.fail_repairs
                    )
                    value = 41 if incorrect else 42
                    (cwd / "answer.py").write_text(f"# {task}\ndef answer():\n    return {value}\n")
                else:
                    (cwd / "independent.py").write_text(f"value = {len(self.calls)}\n")
                return {"summary": "Implemented approved files", "limitations": []}
            return {"findings": [], "limitations": []}
        finally:
            self.active -= 1

    async def execute(self, argv: list[str], cwd: Path):
        self.commands.append(argv)
        if argv[0] == "gh" or argv[:2] == ["git", "push"]:
            pytest.fail("Publication attempted before human delivery approval")
        result = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return result.returncode, result.stdout, result.stderr


async def drive(c: FactoryController, runner: ScriptedRunner, *, approve_plan: bool = True):
    workflow = await asyncio.to_thread(FactoryWorkflow, c, runner)
    task = asyncio.create_task(workflow.run())
    approvals = []
    while not task.done() or not c.events.empty():
        try:
            event = await asyncio.wait_for(c.events.get(), timeout=0.1)
        except TimeoutError:
            continue
        if isinstance(event, ev.InterruptRequest):
            approvals.append(event.payload)
            approve = approve_plan and event.payload["approval_kind"] == "plan"
            if approve:
                assert not any(role == "developer" for role, _ in runner.calls)
                assert worktree._git(["status", "--porcelain"], cwd=Path(c.record["root"]))[1] == ""
            event.future.set_result({"response": {"approved": approve}})
    await task
    return approvals


@pytest.mark.timeout(60)
@pytest.mark.parametrize("repair", [False, True])
async def test_goal_research_approval_test_first_parallel_repair_and_delivery(
    controller: FactoryController,
    repair: bool,  # noqa: FBT001 - pytest parametrization.
):
    runner = ScriptedRunner(controller, repair=repair)
    approvals = await drive(controller, runner)
    assert [p["approval_kind"] for p in approvals] == ["plan", "delivery"], controller.record[
        "blockers"
    ]
    record = controller.record
    assert record["status"] == "paused", record["blockers"]
    assert record["stage"] == "delivery_approval"
    assert runner.peak >= 2
    check = record["checks"][0]
    assert check["baseline_failed"]
    assert check["status"] == "passed"
    assert len(check["evidence"]) == (3 if repair else 2)
    assert (
        (Path(record["candidate"]["workspace"]) / "answer.py").read_text().endswith("return 42\n")
    )
    assert (Path(record["root"]) / "answer.py").read_text().endswith("return 0\n")
    assert record["publication"]["status"] == "prepared"
    assert "delivery" not in record["approvals"]


@pytest.mark.timeout(30)
async def test_headless_declined_plan_suspends_without_worktree_or_mutation(
    controller: FactoryController,
):
    runner = ScriptedRunner(controller)
    approvals = await drive(controller, runner, approve_plan=False)
    assert len(approvals) == 1
    assert controller.record["workspace"] is None
    assert controller.record["status"] == "paused"
    assert not runner.commands
    assert not any(role == "developer" for role, _ in runner.calls)


@pytest.mark.parametrize(
    "text", ["What is Prime doing?", "Explain the code", "How does it work?", "/prime status"]
)
def test_questions_do_not_start_factory_jobs(text: str):
    assert not is_factory_request(text, SimpleNamespace(prime_enabled=True))


def test_explicit_action_and_resume_are_shared_entry_requests():
    session = SimpleNamespace(prime_enabled=True)
    assert is_factory_request("Fix a regression", session)
    assert is_factory_request("Can you add tests", session)
    session.prime_factory_resume = "run"
    assert is_factory_request("Continue", session)
    session.prime_enabled = False
    assert not is_factory_request("Fix a regression", session)


@pytest.mark.timeout(60)
async def test_three_repair_rounds_can_succeed_but_cannot_be_reset(controller: FactoryController):
    runner = ScriptedRunner(controller, repair=True)
    runner.fail_repairs = 2
    approvals = await drive(controller, runner)
    assert [p["approval_kind"] for p in approvals] == ["plan", "delivery"]
    check = controller.record["checks"][0]
    assert check["repairs"] == 3
    assert check["status"] == "passed"
    assert len(check["evidence"]) == 5
    assert (
        len(
            [
                task
                for role, task in runner.calls
                if role == "developer" and task.startswith("independent")
            ]
        )
        == 1
    )


@pytest.mark.timeout(60)
async def test_unsuccessful_repair_limit_blocks_delivery(controller: FactoryController):
    runner = ScriptedRunner(controller, repair=True)
    runner.fail_repairs = 99
    approvals = await drive(controller, runner)
    assert [p["approval_kind"] for p in approvals] == ["plan"]
    assert controller.record["checks"][0]["repairs"] == 3
    assert "three-round" in controller.record["blockers"][0]
    assert controller.record["publication"] is None


@pytest.mark.timeout(30)
async def test_shared_loop_headless_factory_gate_is_mandatory(
    controller: FactoryController,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    import json

    from novacode_cli.headless.runner import EXIT_APPROVAL_REQUIRED, run_headless
    from tests.test_headless import _headless_state

    monkeypatch.setattr("novacode_cli.prime_factory.runtime.SpecialistRunner", ScriptedRunner)
    session = _headless_state()
    session.prime_enabled = True
    session.prime_report = None
    session.workspace_root = controller.record["root"]
    session.headless_prompt = "Fix the answer"
    graph = SimpleNamespace(
        factory_model_route={"provider": "offline", "model": "scripted"}, aupdate_state=AsyncMock()
    )
    code = await run_headless(agent=graph, assistant_id="nova-agent", session_state=session)
    result = json.loads(capsys.readouterr().out)
    assert code == EXIT_APPROVAL_REQUIRED
    assert session.auto_approve is True
    assert session.prime_report["status"] == "paused"
    assert "Continue with /prime resume" in result["result"]
    assert not session.prime_report["workspace"]
    assert graph.aupdate_state.await_count == 2
    assert graph.aupdate_state.await_args_list[0].args[1]["messages"][0].content == "Fix the answer"


def test_routes_inherit_bound_session_and_cli_override_without_fallback():
    from novacode_cli.prime_factory.runtime import model_routes

    profile = SimpleNamespace(
        get_model_config=lambda: {"provider": "saved", "model": "old"},
        get_factory_model=lambda _role: None,
    )
    session = SimpleNamespace(session_model_provider="session")
    graph = SimpleNamespace(factory_model_route={"provider": "session", "model": "current"})
    assert model_routes(profile, session, graph)["developer"]["model"] == "current"
    session.cli_model_override = "session:cli-model"
    assert model_routes(profile, session, graph)["developer"]["model"] == "cli-model"


@pytest.mark.parametrize("installed", ["1.2", "2.0"])
async def test_research_versions_match_actual_isolated_environment(
    controller: FactoryController,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: str,
):
    environment = tmp_path / "execution-env"
    metadata = environment / "Lib" / "site-packages" / f"widget-{installed}.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text(f"Name: Widget\nVersion: {installed}\n", encoding="utf-8")
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"test fixture; never executed")
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "9.9")
    workflow = await asyncio.to_thread(FactoryWorkflow, controller, ScriptedRunner(controller))
    controller.record["workspace"] = await asyncio.to_thread(
        workflow.workspace.prepare, controller.record["id"]
    )
    controller.record.update(
        environment_path=str(environment), research_requirements={"widget": "1.2"}
    )
    controller.record["approvals"]["plan"] = "old-plan"
    if installed == "1.2":
        await workflow.validate_research_environment()
        assert controller.record["approvals"]["plan"] == "old-plan"
    else:
        with pytest.raises(FactoryError, match=r"widget changed to 2\.0"):
            await workflow.validate_research_environment()
        assert not controller.record["approvals"]


@pytest.mark.parametrize(
    "text",
    [
        "Update me on progress",
        "Can you update me on the status?",
        "Test results?",
        "Explain the current code",
    ],
)
def test_status_questions_do_not_resume_prepared_work(text: str):
    session = SimpleNamespace(prime_enabled=True, prime_factory_resume="saved")
    assert not is_factory_request(text, session)


@pytest.mark.timeout(30)
async def test_clear_during_factory_run_cannot_restore_report_or_write_into_new_chat(
    controller: FactoryController,
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.prime_factory.runtime import iterate_factory_events
    from novacode_cli.states.Session import SessionState

    started, release = asyncio.Event(), asyncio.Event()

    class WaitingWorkflow:
        def __init__(self, c: FactoryController, _runner: object) -> None:
            self.c = c

        async def run(self) -> None:
            started.set()
            await release.wait()
            self.c.save()

    monkeypatch.setattr("novacode_cli.prime_factory.runtime.FactoryWorkflow", WaitingWorkflow)
    session = SessionState()
    session.workspace_root = controller.record["root"]
    session.prime_enabled = True
    graph = SimpleNamespace(
        factory_model_route={"provider": "offline", "model": "scripted"}, aupdate_state=AsyncMock()
    )
    original = session.thread_id

    async def consume() -> list:
        return [
            event async for event in iterate_factory_events("Fix the answer", session, agent=graph)
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=10)
    session.reset_conversation()
    release.set()
    events = await task
    assert session.thread_id != original
    assert session.prime_report is None
    assert session.prime_factory is None
    assert session.prime_factory_resume is None
    assert graph.aupdate_state.await_count == 1
    assert graph.aupdate_state.await_args.args[0]["configurable"]["thread_id"] == original
    assert not any(isinstance(event, ev.AssistantMessage) for event in events)


async def test_explicit_resume_rebinds_factory_to_new_originating_conversation(
    controller: FactoryController,
):
    from novacode_cli.prime_factory.runtime import prepare_controller

    controller.request_pause()
    session = SimpleNamespace(
        thread_id="new-conversation", prime_factory_resume=controller.record["id"]
    )
    resumed = await prepare_controller(
        "Continue", session, None, controller.store, Path(controller.record["root"])
    )
    assert resumed.record["thread"] == "new-conversation"
    assert resumed.record["origin_thread"] == "thread"
    assert session.prime_factory is resumed


@pytest.mark.timeout(30)
async def test_clear_during_factory_startup_cannot_adopt_the_new_chat(
    controller: FactoryController,
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.prime_factory.runtime import iterate_factory_events
    from novacode_cli.states.Session import SessionState

    entered, release = threading.Event(), threading.Event()
    session = SessionState()
    session.workspace_root = controller.record["root"]
    session.prime_enabled = True
    graph = SimpleNamespace(aupdate_state=AsyncMock())

    def delayed_directory(_session: object) -> str:
        entered.set()
        assert release.wait(timeout=10)
        return controller.record["root"]

    monkeypatch.setattr("novacode_cli.prime_factory.runtime.session_directory", delayed_directory)

    async def consume() -> list:
        return [
            event async for event in iterate_factory_events("Fix the answer", session, agent=graph)
        ]

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, timeout=10)
        session.reset_conversation()
    finally:
        release.set()
    events = await task
    graph.aupdate_state.assert_not_awaited()
    assert session.prime_factory is None
    assert session.prime_report is None
    assert len(events) == 1
    assert isinstance(events[0], ev.Done)


@pytest.mark.timeout(60)
async def test_specific_human_waiver_retains_missing_scanner_evidence_and_delivery_gate(
    controller: FactoryController,
):
    class ScannerRunner(ScriptedRunner):
        async def run(
            self,
            role: str,
            task: str,
            cwd: Path,
            context: dict[str, Any],
            *,
            files: list[str] | None = None,
        ) -> dict[str, Any]:
            result = await super().run(role, task, cwd, context, files=files)
            if role == "test_engineer" and files is None:
                result["checks"].append(
                    {
                        "argv": ["missing-scanner"],
                        "purpose": "Security scanner",
                        "criteria": [0],
                        "security": True,
                        "tasks": ["answer"],
                    }
                )
            return result

        async def execute(self, argv: list[str], cwd: Path) -> tuple[int | None, str, str]:
            if argv == ["missing-scanner"]:
                return None, "", "scanner unavailable"
            return await super().execute(argv, cwd)

    runner = ScannerRunner(controller)
    approvals = await drive(controller, runner)
    assert [item["approval_kind"] for item in approvals] == ["plan"]
    check = controller.record["checks"][1]
    assert check["status"] == "unavailable"
    identity = controller.record["candidate"]["id"]
    controller.waive(check["id"], "Human accepts this specific unavailable offline scanner")
    assert controller.waived(check["id"], identity)
    assert not controller.waived(check["id"], "changed-candidate")
    await controller.resume()
    approvals = await drive(controller, runner)
    assert [item["approval_kind"] for item in approvals] == ["delivery"]
    assert check["status"] == "waived"
    assert check["evidence"][-1]["exit_code"] is None
    assert "unverified; human waiver" in controller.record["publication"]["body"]
    assert "delivery" not in controller.record["approvals"]
