"""Real LangGraph invocation through the canonical loop, without provider calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.states.Session import SessionState
from novacode_cli.teams.adapters.inprocess import InProcessAdapter
from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.gateway import TEAM_TOOLS, Binding, current_binding, team_create
from novacode_cli.teams.middleware import TeamMiddleware
from novacode_cli.teams.models import Identity, SpawnRequest

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, _tools: list, **_kwargs: object):
        return self


async def test_real_inprocess_worker_inherits_context_and_keeps_private_checkpoint(tmp_path: Path):
    built = []

    def builder(request: SpawnRequest, binding: Binding) -> tuple:
        state = SessionState()
        state.thread_id = state.session_id = request.identity.agent
        graph = create_agent(
            ScriptedModel(responses=[AIMessage("Verified SQLite constraints.")]),
            middleware=[TeamMiddleware(binding)],
            checkpointer=InMemorySaver(),
        )
        built.append((graph, state))
        return graph, None, state

    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"default": InProcessAdapter(builder)})
    team = await host.request(host.lead, "team_create", {"name": "implementation"})
    worker = await host.spawn(
        host.lead,
        "verify the task",
        team["id"],
        messages=[HumanMessage("Correction: SQLite only.")],
    )
    graph, state = built[0]
    await asyncio.wait_for(host._handles[worker["id"]].queue.join(), timeout=20)
    record = await host.request(host.lead, "get_agent", {"agent_id": worker["id"]})
    assert record["status"] == "idle"
    assert "Verified SQLite" in record["summary"]
    snapshot = await graph.aget_state({"configurable": {"thread_id": state.thread_id}})
    assert any("SQLite only" in str(message.content) for message in snapshot.values["messages"])
    assert current_binding.get() is None
    await host.close()
    assert host._handles == {}


async def test_unbound_gateway_does_not_create_a_team():
    assert "error" in await team_create.ainvoke({"name": "forged"})
    assert all("identity" not in str(tool.args) for tool in TEAM_TOOLS)


def test_disabled_bootstrap_keeps_tools_and_creates_no_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.teams import bootstrap

    monkeypatch.setattr(bootstrap, "enabled", lambda: False)
    tools = []
    result, middleware, coordinator = bootstrap.attach(None, "nova", tools, [], tmp_path, "root")
    assert result is tools
    assert middleware is None
    assert coordinator is None
    assert list(tmp_path.iterdir()) == []


async def test_enabled_bootstrap_registers_real_tools_and_scoped_legacy_roster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.config.config import settings
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.teams import bootstrap

    monkeypatch.setattr(bootstrap, "enabled", lambda: True)
    monkeypatch.setattr(NovaConfig, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(NovaConfig, "get_model_base_url", lambda _self: None)
    monkeypatch.setattr(NovaConfig, "get_openai_auth_mode", lambda _self: "api_key")
    monkeypatch.setattr(type(settings), "user_deepagents_dir", property(lambda _self: tmp_path))
    model = ScriptedModel(
        metadata={"nova_provider": "scripted", "nova_model": "offline"},
        responses=[
            AIMessage(
                "",
                tool_calls=[{"id": "create", "name": "team_create", "args": {"name": "offline"}}],
            ),
            AIMessage("Created."),
        ],
    )
    from novacode_cli.agents.agent_file import FilesystemToolFilter

    specs = [
        {
            "name": "general-purpose",
            "tools": [],
            "system_prompt": "Verify constraints.",
            "middleware": [FilesystemToolFilter({"read_file", "grep"})],
        }
    ]
    tools, middleware, host = bootstrap.attach(model, "nova", [], specs, tmp_path, "root")
    assert specs[0]["middleware"][-1].agent_name == "general-purpose"
    assert "read_file" in host.initial.capabilities["general-purpose"]
    assert "write_file" not in host.initial.capabilities["general-purpose"]
    state = SessionState()
    state.session_id = "root"
    host.bind_session(state)
    graph = create_agent(model, tools, middleware=[middleware], checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"messages": [HumanMessage("Create the team.")]},
        {"configurable": {"thread_id": state.thread_id}},
    )
    assert result["messages"][-1].content == "Created."
    assert len(await host.initial._db("teams", host.initial.lead)) == 1
    # Exercise bootstrap's actual teammate builder, including callback attachment.
    from novacode_cli.agents import core_agent

    def configured_graph(
        selected: object, _assistant: str, _tools: list, **options: object
    ) -> tuple:
        child_graph = create_agent(
            selected,
            TEAM_TOOLS,
            middleware=[TeamMiddleware(options["team_binding"])],
            checkpointer=InMemorySaver(),
        )
        return child_graph, None

    monkeypatch.setattr(core_agent, "create_agent_with_config", configured_graph)
    team = (await host.initial._db("teams", host.initial.lead))[0]
    child = await host.initial.spawn(host.initial.lead, "Verify callback attribution.", team["id"])
    await asyncio.wait_for(host.initial._handles[child["id"]].queue.join(), timeout=10)
    child_record = await host.initial.request(
        host.initial.lead, "get_agent", {"agent_id": child["id"]}
    )
    assert child_record["usage_totals"]["calls"] == 2
    await host.release_session(state.thread_id)


async def test_peer_message_during_tool_arrives_once_before_next_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from langchain.tools import tool
    from langchain_core.messages import ToolMessage

    from novacode_cli.security import policy

    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    built = []
    monkeypatch.setattr(
        policy,
        "get_policy",
        lambda *_a, **_k: policy.ApprovalPolicy(
            tool_tiers={"slow_read": "allow"},
            shell_allow=[],
            shell_deny=[],
            path_allow=[],
            path_deny=[],
            domain_allow=[],
            domain_deny=[],
        ),
    )

    @tool
    async def slow_read() -> str:
        """Wait for an offline file observation."""
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return "exact file contents"

    def builder(request: SpawnRequest, binding: Binding) -> tuple:
        state = SessionState()
        state.thread_id = state.session_id = request.identity.agent
        graph = create_agent(
            ScriptedModel(
                responses=[
                    AIMessage("", tool_calls=[{"name": "slow_read", "args": {}, "id": "read"}]),
                    AIMessage("Verified."),
                ]
            ),
            [slow_read],
            middleware=[TeamMiddleware(binding)],
            checkpointer=InMemorySaver(),
        )
        built.append((graph, state))
        return graph, None, state

    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"default": InProcessAdapter(builder)})
    team = await host.request(host.lead, "team_create", {"name": "messages"})
    worker = await host.spawn(host.lead, "Read once.", team["id"])
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await host.request(
            host.lead,
            "send_message",
            {
                "team_id": team["id"],
                "to_agent": worker["id"],
                "message": "Peer evidence: SQLite only.",
                "request_id": "peer",
            },
        )
        release.set()
        await asyncio.wait_for(host._handles[worker["id"]].queue.join(), timeout=10)
        graph, state = built[0]
        snapshot = await graph.aget_state({"configurable": {"thread_id": state.thread_id}})
        assert count == 1
        messages = snapshot.values["messages"]
        assert (
            sum(
                isinstance(message, ToolMessage) and message.tool_call_id == "read"
                for message in messages
            )
            == 1
        )
        assert sum("SQLite only" in str(message.content) for message in messages) == 1
        assert current_binding.get() is None
    finally:
        release.set()
        await host.close()
