"""Real LangGraph invocation through the canonical loop, without provider calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
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
    lifecycle = host.drain_events()
    assert lifecycle and {event.phase_kind for event in lifecycle} == {"team"}
    assert any(event.status == "done" for event in lifecycle)
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

    from langgraph.store.memory import InMemoryStore
    monkeypatch.setattr(core_agent, "get_shared_store", lambda: InMemoryStore())
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


async def test_profile_gates_attach_refresh_and_preserve_team_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from novacode_cli.config.config import settings
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.teams import bootstrap
    from novacode_cli.teams.models import TeamError

    monkeypatch.setattr(type(settings), "user_deepagents_dir", property(lambda _self: tmp_path))
    profile = NovaConfig()
    model = ScriptedModel(
        metadata={"nova_provider": "scripted", "nova_model": "offline"},
        responses=[AIMessage("Offline only.")],
    )
    tools, middleware, host = bootstrap.attach(model, "nova", [], [], tmp_path, "settings-root")
    assert tools == []
    assert middleware is None
    assert host is None

    profile.set_agent_team_settings(enabled=True, swarm=False)
    tools, middleware, host = bootstrap.attach(model, "nova", [], [], tmp_path, "settings-root")
    assert {tool.name for tool in TEAM_TOOLS} <= {tool.name for tool in tools}
    assert middleware is not None
    coordinator = host.initial
    manual = await coordinator.request(coordinator.lead, "team_create", {"name": "manual"})
    adapter = AsyncMock()
    worker_handle = AsyncMock()
    adapter.start.return_value = worker_handle
    coordinator.adapters["default"] = adapter
    worker = await coordinator.spawn(coordinator.lead, "Keep working", manual["id"])
    with pytest.raises(TeamError, match="swarm"):
        await coordinator.request(coordinator.lead, "team_create", {"name": "auto", "swarm": True})

    try:
        profile.set_agent_team_settings(enabled=True, swarm=True)
        _, _, refreshed = bootstrap.attach(model, "nova", [], [], tmp_path, "settings-root")
        assert refreshed is host
        await coordinator.request(coordinator.lead, "team_create", {"name": "auto", "swarm": True})

        profile.set_agent_team_settings(enabled=False, swarm=False)
        tools, middleware, retained = bootstrap.attach(
            model, "nova", [], [], tmp_path, "settings-root"
        )
        assert tools == []
        assert middleware is None
        assert retained is host
        assert not coordinator.swarm_enabled
        assert not coordinator._closed
        assert coordinator._handles[worker["id"]] is worker_handle
        worker_handle.stop.assert_not_awaited()

        profile.set_agent_team_settings(enabled=True, swarm=False)
        _, _, refreshed = bootstrap.attach(model, "nova", [], [], tmp_path, "settings-root")
        assert refreshed is host
        records = await coordinator.request(
            coordinator.lead, "task_list", {"team_id": manual["id"]}
        )
        assert records is not None  # Existing team remains owned and accessible.
        with pytest.raises(TeamError, match="swarm"):
            await coordinator.request(
                coordinator.lead, "team_create", {"name": "blocked", "swarm": True}
            )
    finally:
        await coordinator.close()


@pytest.mark.timeout(60)
async def test_five_bootstrap_teammates_with_learning_reach_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from langgraph.store.memory import InMemoryStore

    from novacode_cli.agents import core_agent
    from novacode_cli.config.config import settings
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.hermes.middleware import NovaLearningMiddleware
    from novacode_cli.teams import bootstrap

    monkeypatch.setattr(bootstrap, "enabled", lambda: True)
    monkeypatch.setattr(NovaConfig, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(NovaConfig, "get_model_base_url", lambda _: None)
    monkeypatch.setattr(type(settings), "user_deepagents_dir", property(lambda _: tmp_path))
    store = InMemoryStore()
    monkeypatch.setattr(core_agent, "get_shared_store", lambda: store)
    built = []

    def configured_graph(selected, assistant, tools, **options):
        learning = NovaLearningMiddleware(
            options.get("store"), agent_dir=tmp_path, enabled=True, review_mode="periodic"
        )
        graph = create_agent(
            selected,
            middleware=[TeamMiddleware(options["team_binding"]), learning],
            checkpointer=InMemorySaver(),
            store=options.get("store"),
        )
        built.append((graph, learning))
        return graph, None

    monkeypatch.setattr(core_agent, "create_agent_with_config", configured_graph)
    model = ScriptedModel(
        metadata={"nova_provider": "scripted", "nova_model": "offline"},
        responses=[AIMessage("Verified offline research probe.")],
    )
    _, _, host = bootstrap.attach(
        model, "nova", [], [{"name": "general-purpose", "tools": []}], tmp_path, "five-workers"
    )
    try:
        team = await host.initial.request(host.initial.lead, "team_create", {"name": "five-probes"})
        workers = await asyncio.gather(
            *(
                host.initial.spawn(host.initial.lead, "Verify this probe.", team["id"])
                for _ in range(5)
            )
        )
        await asyncio.wait_for(
            asyncio.gather(*(host.initial._handles[w["id"]].queue.join() for w in workers)),
            timeout=30,
        )
        records = [
            await host.initial.request(host.initial.lead, "get_agent", {"agent_id": w["id"]})
            for w in workers
        ]
        assert all(r["status"] == "idle" for r in records), [
            (r["status"], r.get("error")) for r in records
        ]
        assert all(r["usage_totals"]["calls"] > 0 for r in records)
        assert all("Verified offline" in r["summary"] for r in records)
        assert all(r["model"] == "offline" for r in records)
    finally:
        await host.initial.close()


async def test_spawn_tool_launches_teammate_without_starting_agent_server(tmp_path, monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from langchain_core.messages import ToolMessage

    from novacode_cli.agents.async_on_demand import AsyncServerOnDemandMiddleware

    start_server = AsyncMock(side_effect=AssertionError("spawn_agent does not require an agent server"))
    monkeypatch.setattr("novacode_cli.agents.server_launcher.ensure_started", start_server)
    adapter = AsyncMock()
    adapter.start.return_value.stop.return_value = True
    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"default": adapter})
    try:
        team = await host.request(host.lead, "team_create", {"name": "offline-team"})

        async def endpoint(operation, payload):
            return await host.request(host.lead, operation, payload)

        binding = Binding(host.lead, endpoint, host)
        model = ScriptedModel(responses=[
            AIMessage("", tool_calls=[{
                "name": "spawn_agent", "id": "spawn", "args": {
                    "task": "Review the project", "team_id": team["id"],
                },
            }]),
            AIMessage("Teammate launched."),
        ])
        graph = create_agent(model, TEAM_TOOLS, middleware=[
            AsyncServerOnDemandMiddleware(), TeamMiddleware(binding),
        ])
        result = await graph.ainvoke({"messages": [HumanMessage("Launch a teammate")]})
        reply = next(message for message in result["messages"] if isinstance(message, ToolMessage))
        agent = json.loads(reply.content)
        assert "error" not in agent
        assert agent["mode"] == "default"
        assert agent["id"] in host._handles
        adapter.start.assert_awaited_once()
        start_server.assert_not_awaited()
    finally:
        await host.close()
