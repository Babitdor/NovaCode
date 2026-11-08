"""Trusted worker budgets, capability leases and bounded host processes."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from novacode_cli.prime_factory.runner import MAX_COMMAND_OUTPUT, SpecialistRunner
from novacode_cli.prime_factory.state import (
    digest,
    plan_definition,
    validate_checks,
    validate_tasks,
)
from novacode_cli.prime_factory.store import FactoryError
from novacode_cli.prime_factory.worker import FactoryUsageCallback
from novacode_cli.prime_factory.workspace import FactoryWorkspace
from novacode_cli.teams.models import TeamError, operation_digest
from novacode_cli.teams.scheduler import PROCESS_BUDGET

pytest_plugins = ("tests.test_factory_workflow",)

if TYPE_CHECKING:
    from novacode_cli.prime_factory.controller import FactoryController
    from novacode_cli.teams.adapters.process import ProcessAdapter
    from novacode_cli.teams.models import EventCallback, GatewayCallback, SpawnRequest


def approve(c: FactoryController, argv: list[str] | None = None) -> Path:
    c.record.update(
        criteria=["A command reports completion"],
        tasks=validate_tasks(
            [{"id": "code", "description": "Change answer", "files": ["answer.py"]}]
        ),
        checks=validate_checks(
            [{"argv": argv or ["python", "check.py"], "purpose": "Acceptance", "criteria": [0]}],
            ["A command reports completion"],
        ),
        setup=[],
        test_files=[],
        stage="implementation",
    )
    workspace = FactoryWorkspace(Path(c.record["root"]))
    c.record["workspace"] = workspace.prepare(c.record["id"])
    c.approve("plan", digest(plan_definition(c.record)))
    return Path(c.record["workspace"]["path"])


async def test_callback_reserves_before_call_and_unknown_usage_is_conservative(
    controller: FactoryController,
):
    attempt = controller.start_attempt("research", "docs", Path(controller.record["root"]))

    async def endpoint(method: str, payload: dict) -> dict:
        if method == "factory_reserve":
            return await controller.reserve(attempt["id"], payload["call"], payload["tokens"])
        await controller.reconcile(attempt["id"], payload["reservation"], payload["actual"])
        return {}

    callback = FactoryUsageCallback(endpoint)
    await callback.on_chat_model_start(
        {}, [[HumanMessage("Goal")]], run_id="call", invocation_params={"max_tokens": 100}
    )
    reservation = controller.record["reservations"][attempt["id"] + ":call"]["tokens"]
    assert reservation > 100
    response = LLMResult(generations=[[ChatGeneration(message=AIMessage("No usage"))]])
    await callback.on_llm_end(response, run_id="call")
    assert controller.record["usage"]["tokens"] == reservation
    assert not controller.record["reservations"]


async def test_capacity_rejection_prevents_provider_start(controller: FactoryController):
    controller.record["budget"]["tokens"] = 1
    attempt = controller.start_attempt("research", "docs", Path(controller.record["root"]))

    async def endpoint(_method: str, payload: dict) -> dict:
        return await controller.reserve(attempt["id"], payload["call"], payload["tokens"])

    callback = FactoryUsageCallback(endpoint)
    with pytest.raises(FactoryError, match="capacity"):
        await callback.on_chat_model_start(
            {}, [[HumanMessage("Goal")]], run_id="call", invocation_params={"max_tokens": 100}
        )
    assert controller.record["status"] == "paused"


async def test_unknown_output_bound_is_rejected_before_provider_or_host_call():
    endpoint = AsyncMock()
    callback = FactoryUsageCallback(endpoint)
    with pytest.raises(FactoryError, match="enforceable output-token bound"):
        await callback.on_chat_model_start({}, [[HumanMessage("Goal")]], run_id="unknown")
    endpoint.assert_not_awaited()


@pytest.mark.timeout(30)
async def test_host_execution_bounds_output_and_releases_process_capacity(
    controller: FactoryController,
):
    argv = [sys.executable, "-c", "print('x' * 300000)"]
    root = await asyncio.to_thread(approve, controller, argv)
    runner = SpecialistRunner(controller)
    runner.authorize = AsyncMock(return_value=True)
    before = PROCESS_BUDGET.used
    code, output, error = await runner.execute(argv, root)
    assert code == 0, error
    assert len(output) < MAX_COMMAND_OUTPUT + 100
    assert "truncated" in output
    assert PROCESS_BUDGET.used == before
    assert not controller.handles


@pytest.mark.timeout(30)
async def test_pause_terminates_owned_command(controller: FactoryController):
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    root = await asyncio.to_thread(approve, controller, argv)
    runner = SpecialistRunner(controller)
    runner.authorize = AsyncMock(return_value=True)
    before = PROCESS_BUDGET.used
    task = asyncio.create_task(runner.execute(argv, root))
    async with asyncio.timeout(10):
        while not controller.handles:
            if task.done():
                pytest.fail(f"Command exited before pause: {await task}")
            await asyncio.sleep(0.01)
    controller.request_pause()
    with pytest.raises(FactoryError, match="suspended"):
        await task
    assert not controller.handles
    assert PROCESS_BUDGET.used == before


async def test_setup_cannot_publish_under_plan_approval(controller: FactoryController):
    root = await asyncio.to_thread(approve, controller)
    runner = SpecialistRunner(controller)
    with pytest.raises(FactoryError, match="Publication"):
        await runner.execute(["git", "push", "origin", "main"], root)
    with pytest.raises(FactoryError, match="human approval"):
        await runner.publish_execute(["gh", "pr", "create"], root)


async def test_worker_gate_binds_identity_and_leases(
    controller: FactoryController, monkeypatch: pytest.MonkeyPatch
):
    root = await asyncio.to_thread(approve, controller)
    runner = SpecialistRunner(controller)
    runner.authorize = AsyncMock(return_value=True)
    observed = []

    class Handle:
        def __init__(self) -> None:
            self.recovery = {"kind": "absent"}

        async def send(self, _text: str) -> None:
            await event("idle", {"full_output": '{"summary":"ready"}'})

        async def stop(self) -> bool:
            return True

    event = None

    async def start(
        _adapter: ProcessAdapter,
        request: SpawnRequest,
        callback: EventCallback,
        gateway: GatewayCallback,
    ) -> Handle:
        nonlocal event
        event = callback
        assert request.factory["attempt"] == request.identity.agent
        args = {"file_path": str(root / "answer.py"), "content": "approved"}
        payload = {
            "tool": "write_file",
            "args": args,
            "tool_call_id": "write",
            "write_paths": [str(root / "answer.py")],
        }
        allowed = await gateway("authorize", payload)
        assert allowed == {"allowed": True, "digest": operation_digest("write_file", args, "write")}
        observed.append(allowed)
        assert not (await gateway("authorize", payload))["allowed"]
        payload["tool_call_id"] = "other"
        payload["write_paths"] = [str(root / "independent.py")]
        assert not (await gateway("authorize", payload))["allowed"]
        assert "error" in await gateway("team_create", {})
        return Handle()

    monkeypatch.setattr("novacode_cli.prime_factory.runner.ProcessAdapter.start", start)
    result = await runner.run("developer", "code", root, {}, files=["answer.py"])
    assert result["summary"] == "ready"
    assert len(observed) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "--no-pager", "push", "origin", "main"],
        ["gh", "--repo", "owner/repo", "pr", "create"],
        ["/usr/bin/git", "push", "origin", "main"],
    ],
)
async def test_publication_options_cannot_bypass_gate(
    controller: FactoryController, argv: list[str]
):
    root = await asyncio.to_thread(approve, controller)
    runner = SpecialistRunner(controller)
    runner._execute = AsyncMock()
    with pytest.raises(FactoryError, match="Publication"):
        await runner.execute(argv, root)
    runner._execute.assert_not_awaited()


async def test_process_limit_failure_does_not_leave_uncertain_start(
    controller: FactoryController,
    monkeypatch: pytest.MonkeyPatch,
):
    argv = [sys.executable, "-c", "print('done')"]
    root = await asyncio.to_thread(approve, controller, argv)
    runner = SpecialistRunner(controller)
    runner.authorize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        PROCESS_BUDGET, "reserve", Mock(side_effect=TeamError("Process limit reached"))
    )
    with pytest.raises(TeamError, match="Process limit"):
        await runner.execute(argv, root)
    assert controller.record["attempts"][-1]["status"] == "stopped"
    controller.request_pause()
    await controller.resume()
    assert controller.record["status"] == "running"


@pytest.mark.timeout(30)
async def test_resume_reconciles_process_started_before_pid_checkpoint(
    controller: FactoryController,
):
    attempt = controller.start_attempt(
        "research", "interrupted-launch", Path(controller.record["root"])
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        env={**os.environ, "NOVA_TEAM_ATTEMPT": attempt["id"]},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        controller.request_pause()
        await controller.resume()
        await asyncio.wait_for(process.wait(), timeout=5)
        assert attempt["status"] == "stopped"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_start_recovery_does_not_skip_owned_nonce_after_clock_change(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.prime_factory import recovery

    process = Mock(pid=123, info={"username": "owner", "create_time": 95.0})
    process.environ.return_value = {"NOVA_TEAM_ATTEMPT": "owned-nonce"}
    monkeypatch.setattr("psutil.process_iter", lambda *_args, **_kwargs: [process])
    stop = Mock(return_value=True)
    monkeypatch.setattr(recovery, "stop_process", stop)

    assert recovery._find_and_stop({"owner": "owner", "since": 100.0, "attempt": "owned-nonce"})
    stop.assert_called_once_with(
        {
            "kind": "process",
            "pid": 123,
            "born": 95.0,
            "attempt": "owned-nonce",
        }
    )


def test_start_recovery_never_signals_foreign_owner_or_nonce(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.prime_factory import recovery

    foreign_owner = Mock(pid=123, info={"username": "other", "create_time": 100.0})
    foreign_owner.environ.return_value = {"NOVA_TEAM_ATTEMPT": "owned-nonce"}
    foreign_nonce = Mock(pid=124, info={"username": "owner", "create_time": 95.0})
    foreign_nonce.environ.return_value = {"NOVA_TEAM_ATTEMPT": "different-nonce"}
    monkeypatch.setattr(
        "psutil.process_iter", lambda *_args, **_kwargs: [foreign_owner, foreign_nonce]
    )
    stop = Mock(return_value=True)
    monkeypatch.setattr(recovery, "stop_process", stop)

    assert recovery._find_and_stop({"owner": "owner", "since": 100.0, "attempt": "owned-nonce"})
    stop.assert_not_called()
    foreign_owner.environ.assert_not_called()


@pytest.mark.parametrize(("born", "expected"), [(95.0, True), (100.0, False)])
def test_start_recovery_preserves_protected_process_uncertainty(
    monkeypatch: pytest.MonkeyPatch, born: float, *, expected: bool
):
    import psutil

    from novacode_cli.prime_factory import recovery

    process = Mock(pid=123, info={"username": "owner", "create_time": born})
    process.environ.side_effect = psutil.AccessDenied(123)
    monkeypatch.setattr("psutil.process_iter", lambda *_args, **_kwargs: [process])
    stop = Mock(return_value=True)
    monkeypatch.setattr(recovery, "stop_process", stop)
    assert (
        recovery._find_and_stop(
            {
                "owner": "owner",
                "since": 100.0,
                "attempt": "owned-nonce",
            }
        )
        is expected
    )
    stop.assert_not_called()
