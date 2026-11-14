"""A model switch / MCP reload must rebuild the agent on the SAME backend.

The rebuild passed ``sandbox=self._backend if hasattr(self._backend, "default")``
— but ``_backend`` is the CompositeBackend, which always has ``.default``. So in
local mode, after the first /model switch, core_agent wrapped the local
filesystem as a ``/workspace`` sandbox: read_file/edit_file failed with
"unexpected server response: <no output>" and write_file reported success while
the file landed in a stray ``<project>/workspace/`` folder. Found in the saved
sessions of three projects.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

import novacode_cli.agents.core_agent as core_agent
from novacode_cli.states.slices.agent_runtime import AgentRuntimeState

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def builds(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def _fake_build(**kw):  # noqa: ANN003, ANN202
        seen.append(kw)
        return object(), SimpleNamespace(default=object())

    monkeypatch.setattr(core_agent, "create_agent_with_config", _fake_build)
    monkeypatch.setattr("novacode_cli.mcp.reset_shared_mcp_middleware", lambda: None)
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", AsyncMock)
    return seen


def _runtime(sandbox_type: str | None, sandbox: object | None) -> AgentRuntimeState:
    rt = AgentRuntimeState()
    rt.set_agent_context(
        agent=object(),
        backend=SimpleNamespace(default=object()),  # the composite
        checkpointer=object(),
        store=object(),
        tools=[],
        assistant_id="a",
        model=object(),
        sandbox_type=sandbox_type,
        sandbox=sandbox,
    )
    return rt


@pytest.mark.parametrize("rebuild", ["switch_model", "reload_mcp_servers"])
def test_local_mode_stays_local_after_a_rebuild(builds, rebuild) -> None:
    rt = _runtime("none", sandbox=None)
    if rebuild == "switch_model":
        asyncio.run(rt.switch_model(object()))
    else:
        asyncio.run(rt.reload_mcp_servers())
    assert builds[-1]["sandbox"] is None, "the composite was passed off as a sandbox"
    # A second rebuild must not pick up the NEW composite either.
    asyncio.run(rt.switch_model(object()))
    assert builds[-1]["sandbox"] is None


def test_a_real_sandbox_is_reused_not_the_composite(builds) -> None:
    docker = object()
    rt = _runtime("docker", sandbox=docker)
    asyncio.run(rt.switch_model(object()))
    assert builds[-1]["sandbox"] is docker
    assert builds[-1]["exec_sandbox"] is False


def test_os_confinement_survives_a_rebuild(builds) -> None:
    rt = _runtime("os", sandbox=None)
    asyncio.run(rt.switch_model(object()))
    assert builds[-1]["exec_sandbox"] is True, "the shell lost its OS sandbox"


@pytest.mark.parametrize("rebuild", ["switch_model", "reload_mcp_servers"])
def test_rebuild_preserves_the_shared_empty_steering_list(builds: list[dict], rebuild: str) -> None:
    from novacode_cli.bootstrap.steering import SteeringInstruction, SteeringMiddleware

    runtime = _runtime("none", sandbox=None)
    shared = []
    if rebuild == "switch_model":
        asyncio.run(runtime.switch_model(object(), steering_instructions=shared))
    else:
        asyncio.run(runtime.reload_mcp_servers(steering_instructions=shared))
    assert builds[-1]["steering_instructions"] is shared
    middleware = SteeringMiddleware(instructions=builds[-1]["steering_instructions"])
    shared.append(SteeringInstruction("steer", "Apply my correction", live=True))
    assert "Apply my correction" in middleware.before_model({}, None)["messages"][0].content


@pytest.mark.parametrize(
    "operation", ["reload_mcp_servers", "switch_model", "reload_agent_definitions"]
)
async def test_session_rebuild_preserves_identity_workspace_and_options(
    builds: list[dict], tmp_path: Path, operation: str
) -> None:
    from novacode_cli.states.Session import SessionState

    state = SessionState()
    options = {
        "system_prompt": "Session-specific instructions",
        "extra_middleware": [object()],
        "exec_sandbox": True,
        "auto_approve": True,
    }
    runtime = _runtime("none", None)
    runtime._agent = SimpleNamespace(
        agent_definitions_workspace=str(tmp_path), agent_definition_options=options
    )
    state._agent_runtime = runtime
    if operation == "switch_model":
        await state.switch_model(object())
    else:
        await getattr(state, operation)()
    assert builds[-1]["session_id"] == state.session_id
    assert builds[-1]["workspace_root"] == str(tmp_path)
    for key, value in options.items():
        assert builds[-1][key] == value
    assert builds[-1]["checkpointer"] is runtime._checkpointer
    assert builds[-1]["store"] is runtime._store
    assert builds[-1]["steering_instructions"] is state.steering_instructions


async def test_plugin_reload_then_chat_reuses_live_team_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.config.config import settings
    from novacode_cli.states.Session import SessionState
    from novacode_cli.teams import bootstrap
    from tests.test_team_inprocess import ScriptedModel

    monkeypatch.setattr(bootstrap, "enabled", lambda: True)
    monkeypatch.setattr(type(settings), "user_deepagents_dir", property(lambda _: tmp_path))
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", AsyncMock)
    monkeypatch.setattr("novacode_cli.mcp.reset_shared_mcp_middleware", lambda: None)
    model = ScriptedModel(
        metadata={"nova_provider": "scripted", "nova_model": "offline"},
        responses=[AIMessage("Still available") for _ in range(4)],
    )
    state = SessionState()
    saver = InMemorySaver()
    hosts = []

    def build(**kwargs):  # noqa: ANN003, ANN202 -- Stand-in for graph assembly only.
        from pathlib import Path

        _, middleware, host = bootstrap.attach(
            kwargs["model"],
            "nova",
            [],
            [],
            Path(kwargs.get("workspace_root") or tmp_path),
            kwargs.get("session_id"),
        )
        hosts.append(host)
        graph = create_agent(kwargs["model"], middleware=[middleware], checkpointer=saver)
        graph.team_coordinator = host
        graph.agent_definitions_workspace = str(tmp_path)
        graph.agent_definition_options = {}
        return graph, object()

    monkeypatch.setattr(core_agent, "create_agent_with_config", build)
    graph, backend = build(model=model, session_id=state.session_id)
    state.set_agent_context(graph, backend, saver, object(), [], "nova", model)
    config = {"configurable": {"thread_id": state.thread_id}}
    try:
        for _ in range(3):
            host = graph.team_coordinator
            host.bind_session(state)
            result = await graph.ainvoke({"messages": [HumanMessage("Hello")]}, config)
            assert result["messages"][-1].content == "Still available"
            graph, _ = await state.reload_mcp_servers()
        assert all(host is hosts[0] for host in hosts)
        assert not hosts[0].initial._closed
    finally:
        for host in hosts:
            for coordinator in host.roots.values():
                await coordinator.close()


@pytest.mark.parametrize("operation", ["switch_model", "reload_mcp_servers"])
async def test_rebuild_keeps_the_current_worktree(
    builds: list[dict], tmp_path: Path, operation: str
) -> None:
    runtime = _runtime("none", None)
    runtime._agent = SimpleNamespace(agent_definitions_workspace=str(tmp_path / "original"))
    active = tmp_path / "active-worktree"
    runtime._backend.default = SimpleNamespace(cwd=active)
    if operation == "switch_model":
        await runtime.switch_model(object())
    else:
        await runtime.reload_mcp_servers()
    assert builds[-1]["workspace_root"] == str(active)
