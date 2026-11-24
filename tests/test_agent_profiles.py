"""Composer profiles preserve their tool boundary and worker identity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain.agents.middleware.types import ToolCallRequest

from novacode_cli import ui_events as ev
from novacode_cli.agents.profiles import (
    AgentProfileMiddleware,
    current_profile,
    profile_readiness,
    profile_tool_allowed,
)
from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.core.profile_workers import ProfileWorkers, current_workers, summon_agent
from novacode_cli.core.subagent_tasks import format_context, panel_body
from novacode_cli.mcp.generic import MCPInput, build_mcp_tool, resolve_target


def test_profile_tools_and_mcp_discovery(monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.agents.profiles.trusted_browser_servers",
        lambda: frozenset({"obscura", "browser-use"}),
    )
    servers = [
        ("serena", "find_symbol"),
        ("obscura", "browser_navigate"),
        ("browser-use", "browser_screenshot"),
        ("browser-use", "browser_exec"),
    ]
    tools = [
        SimpleNamespace(
            name=f"{server}_{raw}",
            metadata={"nova_mcp_server": server, "nova_mcp_tool": raw},
        )
        for server, raw in servers
    ]
    mcp = SimpleNamespace(
        tools=tools,
        server_status={server: "ready" for server, _raw in servers},
        _tools_cache=[
            {"server": server, "skill_alias": raw, "name": tool.name, "description": raw}
            for (server, raw), tool in zip(servers, tools, strict=True)
        ],
    )
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", lambda: mcp)
    assert profile_readiness("serena") is None
    assert profile_readiness("nora") is None
    mcp.server_status["obscura"] = "cached"
    assert "/mcp" in profile_readiness("nora")
    token = current_profile.set("serena")
    try:
        catalog = build_mcp_tool(mcp).invoke({"action": "list"})
        assert [entry["server_name"] for entry in catalog["tools"]] == ["serena"]
        assert (
            resolve_target(mcp, MCPInput(server_name="obscura", tool_name="browser_navigate"))
            is None
        )
    finally:
        current_profile.reset(token)
    assert profile_tool_allowed("nora", tools[1].name, tools[1].metadata)
    assert profile_tool_allowed("nora", tools[2].name, tools[2].metadata)
    assert not profile_tool_allowed("nora", tools[3].name, tools[3].metadata)
    assert not profile_tool_allowed("ralph", "spawn_agent")
    assert not profile_tool_allowed("ralph", "task")
    assert not profile_tool_allowed("ralph", "start_async_task")
    assert not profile_tool_allowed("ralph", "task_create")


async def test_worker_runs_concurrently_and_reports_its_context(monkeypatch):
    from novacode_cli.states.Session import SessionState

    session = SessionState()
    session.token_tracker = SimpleNamespace(context_window_size=1_000_000)
    released = asyncio.Event()
    started = asyncio.Event()
    events = []

    async def stream(prompt, _agent, _assistant, child, **_kwargs):
        assert prompt == "Read the docs"
        assert child.agent_profile == "nora"
        assert child.thread_id != session.thread_id
        started.set()
        await released.wait()
        yield ev.ContextUsage(129000)
        yield ev.AssistantMessage("Evidence", agent_name="Nora", agent_color=None)
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.core.agent_loop.iterate_agent_events", stream)
    monkeypatch.setattr("novacode_cli.core.profile_workers.profile_readiness", lambda _: None)
    workers = ProfileWorkers(object(), None, None, session, None)
    worker_token = current_workers.set(workers)
    plan_token = active_plan_session.set(session)
    try:
        runtime = SimpleNamespace(stream_writer=lambda frame: events.append(frame["event"]))
        result = await summon_agent.coroutine(runtime, agent="nora", task="Read the docs")
        await asyncio.wait_for(started.wait(), 2)
        assert result["status"] == "running"
        assert not workers.tasks[result["worker_id"]].done()
        released.set()
        result = await summon_agent.coroutine(runtime, worker_id=result["worker_id"])
        assert result == {"agent": "Nora", "status": "done", "result": "Evidence"}
        row = events[-1]
        assert row.status == "done"
        expected = "ctx 129k/1.0M · 13% · 87% left"
        assert format_context(row.context_used, row.context_window) == expected
        assert any(expected in line.plain for line in panel_body([row], width=100, now=0).lines)

        session.agent_profile = "ralph"
        request = ToolCallRequest(
            tool_call={"name": "summon_agent", "args": {}, "id": "blocked"},
            tool=None,
            state={},
            runtime=None,
        )
        assert AgentProfileMiddleware._rejection(request).status == "error"
    finally:
        await workers.close()
        active_plan_session.reset(plan_token)
        current_workers.reset(worker_token)


async def test_summon_tool_and_profile_persist_through_graph():
    import json

    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.states.Session import SessionState
    from tests.test_prime import Model

    class AgentModel(Model):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            from langchain_core.outputs import ChatGeneration, ChatResult

            if messages[-1].text == "Check the documentation":
                reply = AIMessage("Worker evidence")
            elif isinstance(messages[-1], ToolMessage) and messages[-1].tool_call_id == "start":
                worker_id = json.loads(messages[-1].text)["worker_id"]
                reply = AIMessage(
                    "",
                    tool_calls=[
                        {
                            "id": "wait",
                            "name": "summon_agent",
                            "args": {"worker_id": worker_id},
                        }
                    ],
                )
            elif isinstance(messages[-1], ToolMessage):
                assert "Worker evidence" in messages[-1].text
                reply = AIMessage("Collected evidence")
            else:
                reply = AIMessage(
                    "",
                    tool_calls=[
                        {
                            "id": "start",
                            "name": "summon_agent",
                            "args": {"agent": "nova", "task": "Check the documentation"},
                        }
                    ],
                )
            return ChatResult(generations=[ChatGeneration(message=reply)])

    graph = create_agent(
        AgentModel(responses=[]),
        [summon_agent],
        middleware=[AgentProfileMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = SessionState()
    session.agent_profile = "nova"
    events = [event async for event in iterate_agent_events("Delegate", graph, None, session)]
    assert [(e.status, e.subagent_type) for e in events if isinstance(e, ev.SubagentTask)] == [
        ("running", "Nova"),
        ("done", "Nova"),
    ]
    assert any(
        e.text == "Collected evidence" and e.agent_name == "Nova"
        for e in events
        if isinstance(e, ev.AssistantMessage)
    )
    saved = await graph.aget_state({"configurable": {"thread_id": session.thread_id}})
    assert saved.values["messages"][-1].additional_kwargs["nova_agent_profile"] == "nova"


async def test_composer_agent_picker_keeps_draft_and_rejects_locked_profile(monkeypatch):
    from textual.widgets import Button

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.screens import PickScreen
    from novacode_cli.tui.widgets import PromptInput
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _FakeAgent, _SS, _wait_until

    monkeypatch.setattr(
        "novacode_cli.agents.profiles.profile_readiness",
        lambda profile: "Enable Serena in /mcp." if profile == "serena" else None,
    )
    state = _SS()
    app = NovaApp(
        agent=_FakeAgent(),
        session_state=state,
        assistant_id="nova-agent",
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )
    async with app.run_test(size=(100, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "Unsent draft"
        button = app.query_one("#agent-toggle", Button)
        button.press()
        assert await _wait_until(pilot, lambda: isinstance(app.screen, PickScreen))
        await pilot.press("down", "enter")
        assert await _wait_until(pilot, lambda: not isinstance(app.screen, PickScreen))
        assert getattr(state, "agent_profile", None) is None
        assert prompt.text == "Unsent draft"

        button.press()
        assert await _wait_until(pilot, lambda: isinstance(app.screen, PickScreen))
        await pilot.press("end", "enter")
        assert await _wait_until(pilot, lambda: getattr(state, "agent_profile", None) == "nora")
        assert "Nora" in str(button.label)
        assert prompt.text == "Unsent draft"
