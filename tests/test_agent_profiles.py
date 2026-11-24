"""Composer profiles preserve their tool boundary and worker identity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import SystemMessage

from novacode_cli import ui_events as ev
from novacode_cli.agents.profiles import (
    AgentProfileMiddleware,
    autoresearch_playbook,
    current_profile,
    profile_readiness,
    profile_tool_allowed,
)
from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.core.profile_workers import ProfileWorkers, current_workers, summon_agent
from novacode_cli.core.subagent_tasks import format_context, panel_body
from novacode_cli.mcp.generic import MCPInput, build_mcp_tool, resolve_target


@pytest.mark.parametrize(
    ("profile", "planning", "has_tool", "can_summon"),
    [
        (None, False, True, True),
        ("nova", False, True, True),
        ("nora", False, True, True),
        ("serena", False, True, True),
        ("ralph", False, True, False),
        ("nova", True, True, False),
        ("nora", False, False, False),
    ],
)
def test_delegation_guidance_matches_profile_and_mode(profile, planning, has_tool, can_summon):
    from tests.test_prime import Model

    token = active_plan_session.set(
        SimpleNamespace(agent_profile=profile, plan_mode_enabled=planning)
    )
    request = ModelRequest(
        model=Model(responses=[]),
        tools=[summon_agent] if has_tool else [],
        messages=[],
        system_message=SystemMessage("Base instructions"),
        state={},
    )
    try:
        prepared = AgentProfileMiddleware._prepare(request)
    finally:
        active_plan_session.reset(token)
    prompt = prepared.system_message.text
    assert "Base instructions" in prompt
    assert ("Use summon_agent" in prompt) == can_summon
    if can_summon:
        assert "Ralph for autonomous loops or autoresearch" in prompt
        assert "Nora for web research" in prompt
        assert "Serena for precise repository navigation" in prompt
        assert "Nova for broader implementation" in prompt
    elif profile == "ralph":
        assert not prepared.tools
        assert "give the requesting agent a brief" in prompt


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
    assert profile_readiness("nora") is None
    mcp.server_status["browser-use"] = "cached"
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


def test_nora_discovers_trusted_research_tools_and_reads_local_sources(monkeypatch):
    from novacode_cli.agents.code_mode import _native_tools

    monkeypatch.setattr(
        "novacode_cli.agents.profiles.trusted_browser_servers",
        lambda: frozenset({"tavily", "exa", "parallel", "firecrawl"}),
    )
    tools = [
        SimpleNamespace(
            name=f"{server}_{raw}",
            metadata={"nova_mcp_server": server, "nova_mcp_tool": raw},
        )
        for server, raw in [
            ("tavily", "tavily_extract"),
            ("exa", "web_fetch_exa"),
            ("parallel", "web_search"),
            ("firecrawl", "firecrawl_extract"),
            ("tavily", "unknown"),
            ("untrusted", "web_search"),
        ]
    ]
    mcp = SimpleNamespace(
        tools=tools,
        _tools_cache=[
            {
                "server": t.metadata["nova_mcp_server"],
                "skill_alias": t.metadata["nova_mcp_tool"],
                "name": t.name,
                "description": t.name,
            }
            for t in tools
        ],
    )
    token = current_profile.set("nora")
    try:
        catalog = build_mcp_tool(mcp).invoke({"action": "list"})
        assert [entry["name"] for entry in catalog["tools"]] == [t.name for t in tools[:4]]
        request = MCPInput(server_name="tavily", tool_name="tavily_extract")
        assert resolve_target(mcp, request) is tools[0]
        read_file = autoresearch_playbook.model_copy(update={"name": "read_file"})
        write_file = autoresearch_playbook.model_copy(update={"name": "write_file"})
        request = SimpleNamespace(runtime=SimpleNamespace(tools=[read_file, write_file]))
        assert set(_native_tools(request)) == {"read_file"}
    finally:
        current_profile.reset(token)


def test_ralph_can_load_packaged_playbook_through_code_mode():
    from novacode_cli.agents.code_mode import _native_tools

    request = SimpleNamespace(runtime=SimpleNamespace(tools=[autoresearch_playbook, summon_agent]))
    token = current_profile.set("ralph")
    try:
        calls = _native_tools(request)
        assert set(calls) == {"autoresearch_playbook"}
        playbook = calls["autoresearch_playbook"].invoke({})
        assert "baseline" in playbook
        assert "stopping condition" in playbook
    finally:
        current_profile.reset(token)
    assert not profile_tool_allowed("nora", "autoresearch_playbook")


async def test_worker_runs_concurrently_and_reports_its_context(monkeypatch):
    from novacode_cli.states.Session import SessionState

    session = SessionState()
    session.token_tracker = SimpleNamespace(context_window_size=1_000_000)
    released = asyncio.Event()
    started = asyncio.Event()

    async def stream(prompt, _agent, _assistant, child, **_kwargs):
        assert prompt == "Read the docs"
        assert child.agent_profile == "nora"
        assert child.thread_id != session.thread_id
        yield ev.ToolCall("fetch_url", "Read the official docs", "", call_id="fetch")
        yield ev.ToolResult("200 OK", call_id="fetch")
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
        for profile in (None, "nora", "serena"):
            session.agent_profile = profile
            rejected = await summon_agent.coroutine(agent=profile or "nova", task="Duplicate")
            assert "cannot summon itself" in rejected["error"]
        assert not workers.tasks and workers.events.empty() and workers.budget == [4]
        session.agent_profile = None
        result = await summon_agent.coroutine(agent="nora", task="Read the docs")
        await asyncio.wait_for(started.wait(), 2)
        assert result["status"] == "running"
        assert not workers.tasks[result["worker_id"]].done()
        events = [workers.events.get_nowait() for _ in range(workers.events.qsize())]
        previews = [event for event in events if isinstance(event, ev.SubagentPreview)]
        assert [(p.task_id, p.kind, p.call_id) for p in previews] == [
            (result["worker_id"], "status", ""),
            (result["worker_id"], "tool", "fetch"),
            (result["worker_id"], "result", "fetch"),
        ]
        released.set()
        result = await summon_agent.coroutine(worker_id=result["worker_id"])
        assert result == {"agent": "Nora", "status": "done", "result": "Evidence"}
        row = [workers.events.get_nowait() for _ in range(workers.events.qsize())][-1]
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


async def test_summon_tool_and_profile_persist_through_graph(monkeypatch):
    import json

    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.core.profile_workers import _RESULT_PAGE_CHARS
    from novacode_cli.states.Session import SessionState
    from tests.test_prime import Model

    monkeypatch.setattr("novacode_cli.agents.profiles.profile_readiness", lambda _: None)
    monkeypatch.setattr("novacode_cli.core.profile_workers.profile_readiness", lambda _: None)
    worker_reply = "Start of evidence\n" + ("x" * _RESULT_PAGE_CHARS) + "\nEnd of evidence"

    class AgentModel(Model):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            from langchain_core.outputs import ChatGeneration, ChatResult

            if messages[-1].text == "Check the documentation":
                reply = AIMessage(worker_reply)
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
            elif isinstance(messages[-1], ToolMessage) and messages[-1].tool_call_id == "wait":
                result = json.loads(messages[-1].text)
                assert result["result"] == worker_reply[:_RESULT_PAGE_CHARS]
                assert result["next_offset"] == _RESULT_PAGE_CHARS
                start = next(
                    m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "start"
                )
                reply = AIMessage(
                    "",
                    tool_calls=[
                        {
                            "id": "next-page",
                            "name": "summon_agent",
                            "args": {
                                "worker_id": json.loads(start.text)["worker_id"],
                                "result_offset": result["next_offset"],
                            },
                        }
                    ],
                )
            elif isinstance(messages[-1], ToolMessage):
                result = json.loads(messages[-1].text)
                assert result["result"] == worker_reply[_RESULT_PAGE_CHARS:]
                assert "next_offset" not in result
                reply = AIMessage("Collected evidence")
            else:
                reply = AIMessage(
                    "",
                    tool_calls=[
                        {
                            "id": "start",
                            "name": "summon_agent",
                            "args": {"agent": "nora", "task": "Check the documentation"},
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
        ("running", "Nora"),
        ("done", "Nora"),
    ]
    assert any(
        e.text == "Collected evidence" and e.agent_name == "Nova"
        for e in events
        if isinstance(e, ev.AssistantMessage)
    )
    saved = await graph.aget_state({"configurable": {"thread_id": session.thread_id}})
    assert saved.values["messages"][-1].additional_kwargs["nova_agent_profile"] == "nova"


async def test_worker_approval_survives_parent_graph_resume(monkeypatch):
    import json

    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langchain.tools import tool
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.states.Session import SessionState
    from tests.test_prime import Model

    monkeypatch.setattr("novacode_cli.agents.profiles.profile_readiness", lambda _: None)
    released = asyncio.Event()
    actions = []

    @tool("write_todos")
    async def parent_step() -> str:
        """Release the worker after the parent approval."""
        released.set()
        return "Released"

    @tool
    async def wait_for_parent() -> str:
        """Wait for the parent graph to resume."""
        await released.wait()
        return "Parent resumed"

    @tool
    async def checked_action() -> str:
        """Perform the worker action once after approval."""
        actions.append("ran")
        return "Worker approved"

    def call(name, args=None):
        return AIMessage("", tool_calls=[{"id": name, "name": name, "args": args or {}}])

    class AgentModel(Model):
        def _generate(self, messages, stop=None, run_manager=None, **_kwargs):
            last = messages[-1]
            if last.text == "Delegated task":
                reply = call("wait_for_parent")
            elif isinstance(last, ToolMessage):
                if last.name == "wait_for_parent":
                    reply = call("checked_action")
                elif last.name == "checked_action":
                    reply = AIMessage(last.text)
                elif last.name == "write_todos":
                    launch = next(
                        m for m in messages
                        if isinstance(m, ToolMessage) and m.name == "summon_agent"
                    )
                    worker_id = json.loads(launch.text)["worker_id"]
                    reply = call("summon_agent", {"worker_id": worker_id})
                    reply.tool_calls[0]["id"] = "collect"
                elif last.tool_call_id == "summon_agent":
                    reply = call("write_todos")
                else:
                    assert json.loads(last.text)["result"] == "Worker approved"
                    reply = AIMessage("Collected the approved result")
            else:
                reply = call("summon_agent", {"agent": "nova", "task": "Delegated task"})
            return ChatResult(generations=[ChatGeneration(message=reply)])

    agent = create_agent(
        AgentModel(responses=[]),
        [summon_agent, parent_step, wait_for_parent, checked_action],
        middleware=[
            AgentProfileMiddleware(),
            HumanInTheLoopMiddleware(interrupt_on={"write_todos": True, "checked_action": True}),
        ],
        checkpointer=InMemorySaver(),
    )
    session = SessionState()
    session.agent_profile = "serena"
    approvals = []
    events = []
    async with asyncio.timeout(30):
        async for event in iterate_agent_events("Delegate", agent, None, session):
            events.append(event)
            if isinstance(event, ev.InterruptRequest):
                approvals.extend(ar["name"] for ar in event.payload["action_requests"])
                assert not actions
                event.future.set_result({"decisions": [{"type": "approve"}]})
    assert approvals == ["write_todos", "checked_action"]
    assert actions == ["ran"]
    assert any(
        isinstance(event, ev.AssistantMessage) and event.text == "Collected the approved result"
        for event in events
    )


async def test_composer_agent_picker_keeps_draft_and_rejects_locked_profile(monkeypatch):
    from textual.widgets import Button

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.screens import PickScreen
    from novacode_cli.tui.widgets import PromptInput
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent, _wait_until

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
