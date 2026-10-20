"""Lossless handshakes, strict worktree isolation and shared process capacity."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING

import pytest

from novacode_cli.sessions.supervisor import SessionSupervisor
from novacode_cli.teams.adapters.process import ProcessAdapter, strict_worktree
from novacode_cli.teams.models import Identity, SpawnRequest, TeamError
from novacode_cli.teams.protocol import encode_control, validate_handshake
from novacode_cli.teams.scheduler import ProcessBudget

if TYPE_CHECKING:
    from pathlib import Path


def test_control_frames_are_lossless_and_handshake_is_exact():
    value = {"tool": "write_file", "args": {"content": "中" * 100000}}
    assert json.loads(encode_control(value)) == value
    with pytest.raises(TeamError):
        encode_control({"args": "x" * 1100000})
    expected = {
        "version": 1,
        "agent": "a",
        "team": "t",
        "project": "p",
        "root": "r",
        "attempt": "n",
        "model": "configured",
        "provider": "provider",
        "cwd": "/workspace",
        "policy": "hash",
        "base_revision": None,
    }
    validate_handshake(expected, dict(expected))
    for field in expected:
        with pytest.raises(TeamError):
            validate_handshake(expected, {**expected, field: "wrong"})


def test_explicit_worktree_refuses_non_git_workspace(tmp_path: Path):
    with pytest.raises(TeamError, match="isolat"):
        strict_worktree(tmp_path, "worker")


async def test_tab_and_team_supervisors_share_one_process_budget(tmp_path: Path):
    budget = ProcessBudget(1)

    async def event(*_args: object) -> None:
        return None

    first = SessionSupervisor(event, process_budget=budget)
    second = SessionSupervisor(event, process_budget=budget)
    argv = [sys.executable, "-c", "import sys; sys.stdin.read()"]
    await first.spawn(session_id="tab", name="tab", worktree=tmp_path, argv=argv)
    try:
        with pytest.raises((TeamError, RuntimeError)):
            await second.spawn(session_id="agent", name="agent", worktree=tmp_path, argv=argv)
        assert budget.used == 1
    finally:
        await first.close_all(timeout=1)
    assert budget.used == 0
    await second.spawn(session_id="agent", name="agent", worktree=tmp_path, argv=argv)
    await second.close_all(timeout=1)
    assert budget.used == 0


_CHILD = r"""
import sys,json,os
def emit(value): print(json.dumps(value),flush=True)
boot=json.loads(sys.stdin.readline())
assert 'messages' not in boot
emit({'t':'ready','team_handshake':boot})
for line in sys.stdin:
    value=json.loads(line)
    if value['t']=='shutdown':break
    if value['t']=='prompt':
        emit({'t':'ev','c':'AssistantMessage','d':{'text':'fresh:'+value['text'],'agent_name':'worker','agent_color':'cyan'}})
        emit({'t':'turn_done','id':value['id'],'ok':True})
"""


async def test_fork_runs_real_child_with_fresh_messages_and_exact_model(tmp_path: Path):
    done = asyncio.Event()
    observed = []

    async def event(status: str, metadata: dict) -> None:
        observed.append((status, metadata))
        if status == "idle":
            done.set()

    async def gateway(_method: str, _payload: dict) -> dict:
        return {}

    adapter = ProcessAdapter(
        {"provider": "configured-provider", "model": "configured-model"},
        argv=[sys.executable, "-c", _CHILD],
    )
    request = SpawnRequest(
        Identity("p", "r", "worker", "team"), "verify", "fork", "general-purpose", tmp_path
    )
    handle = await adapter.start(request, event, gateway)
    try:
        assert observed == []  # Readiness never starts model work.
        await handle.send("verify")
        await asyncio.wait_for(done.wait(), timeout=10)
        assert observed[-1][1]["summary"] == "fresh:verify"
        assert observed[-1][1]["model"] == "configured-provider:configured-model"
        assert handle.child.proc.pid != __import__("os").getpid()
    finally:
        assert await handle.stop()
    assert handle.child.proc.returncode == 0


async def test_bad_handshake_stops_the_real_child_without_dispatch(tmp_path: Path):
    async def event(*_args: object) -> None:
        return None

    adapter = ProcessAdapter(
        {"provider": "configured", "model": "model"},
        argv=[
            sys.executable,
            "-c",
            _CHILD.replace("assert 'messages' not in boot", "boot['cwd']='wrong workspace'"),
        ],
    )
    request = SpawnRequest(
        Identity("p", "r", "mismatch", "team"), "never run", "fork", "general-purpose", tmp_path
    )
    with pytest.raises(TeamError, match="handshake"):
        await adapter.start(request, event, event)


_NOVA_WORKER = r"""
import os,pathlib
os.environ["HOME"]=os.environ["USERPROFILE"]=str(pathlib.Path.cwd())
from novacode_cli._lazy_heavy import install
install()
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from novacode_cli.config import model_create
from novacode_cli.agents import core_agent
from novacode_cli.teams.middleware import TeamMiddleware
from novacode_cli import main as application
class Model(FakeMessagesListChatModel):
    def bind_tools(self,tools,**kwargs):return self
def model(provider,name):
    return Model(metadata={'nova_provider':provider,'nova_model':name}, responses=[
        AIMessage('Offline Nova worker verified.',usage_metadata={
            'input_tokens':15,'output_tokens':3,'total_tokens':18,
            'input_token_details':{'cache_read':10}})])
def graph(model,assistant_id,tools,**kwargs):
    return create_agent(model, middleware=[TeamMiddleware(kwargs['team_binding'])],
                        checkpointer=InMemorySaver()),None
model_create.build_chat_model=model
core_agent.create_agent_with_config=graph
application._session_tools=lambda state:[]
from novacode_cli.teams.worker import main
raise SystemExit(main())
"""


@pytest.mark.parametrize("result_size", ["small", "large"])
async def test_nova_worker_entrypoint_runs_canonical_loop_and_reports_usage(
    tmp_path: Path, result_size: str
):
    done = asyncio.Event()
    observed = []

    async def event(status: str, metadata: dict) -> None:
        observed.append((status, metadata))
        if status in {"idle", "failed"}:
            done.set()

    async def gateway(_method: str, _payload: dict) -> dict:
        return {"messages": []}

    expected = "Offline Nova worker verified." if result_size == "small" else "é" * 100_000
    script = (
        _NOVA_WORKER
        if result_size == "small"
        else _NOVA_WORKER.replace("'Offline Nova worker verified.'", "'é'*100_000")
    )
    adapter = ProcessAdapter(
        {"provider": "scripted", "model": "offline-model"},
        argv=[sys.executable, "-c", script],
    )
    request = SpawnRequest(
        Identity("p", "r", "nova-worker", "team"), "verify", "fork", "general-purpose", tmp_path
    )
    handle = await adapter.start(request, event, gateway)
    try:
        await handle.send("/auto-approve")  # Literal delegated task, never a CLI command.
        await asyncio.wait_for(done.wait(), timeout=20)
        status, result = observed[-1]
        assert status == "idle", result
        assert result["full_output"] == expected
        from novacode_cli.tracking.usage_tree import UsageTree

        totals = UsageTree.from_dict(result["usage"]).root.totals()
        assert totals["input_tokens"] == 15
        assert totals["cache_read_tokens"] == 10
        assert result["usage_turn"]
    finally:
        assert await handle.stop()
