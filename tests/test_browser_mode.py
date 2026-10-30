"""Browser mode constrains discovery/execution while preserving conversation context."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.agents.browser_mode import BROWSER_PROMPT, BrowserModeMiddleware
from novacode_cli.agents.tool_search import ToolSearchMiddleware
from novacode_cli.config.browser_mode import browser_mode_args, browser_tool_allowed
from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.states.Session import SessionState
from tests.test_prime import Model, call, state
from tests.test_skills_runtime import RecordingModel


@pytest.mark.parametrize(
    "name",
    [
        "execute",
        "read_file",
        "write_file",
        "task",
        "start_async_task",
        "MCPCode",
        "MCPTool",
        "eval",
        "skill_manage",
        "computer_action",
        "enter_plan_mode",
        "obscura_execute",
        "obscura_browser_shell",
        "playwright_browser_navigate",
        "unknown_tool",
    ],
)
async def test_disallowed_calls_never_reach_execution(name: str):
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True, auto_approve=True))
    request = ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "blocked"},
        tool=None,
        state={},
        runtime=None,
    )
    sync, asynchronous = Mock(), AsyncMock()
    try:
        assert BrowserModeMiddleware().wrap_tool_call(request, sync).status == "error"
        assert (
            await BrowserModeMiddleware().awrap_tool_call(request, asynchronous)
        ).status == "error"
        sync.assert_not_called()
        asynchronous.assert_not_called()
    finally:
        active_plan_session.reset(token)


@pytest.mark.parametrize(
    "name",
    [
        "obscura_browser_navigate",
        "obscura_browser_snapshot",
        "obscura_browser_click",
        "obscura_browser_evaluate",
        "web_search",
        "fetch_url",
        "duckduckgo_search",
        "skills_load",
        "ask_user_question",
        "search_tools",
        "write_todos",
    ],
)
def test_browser_capabilities_remain_available(name: str):
    assert browser_tool_allowed(name)


def test_mode_switches_are_mutually_exclusive_and_clear_exits_browser_mode():
    session = SessionState()
    session.plan_mode_enabled = True
    session.browser_mode_enabled = True
    assert not session.plan_mode_enabled
    assert session.browser_mode_enabled
    session.toggle_plan_mode()
    assert session.plan_mode_enabled
    assert not session.browser_mode_enabled
    session.browser_mode_enabled = True
    session.reset_conversation()
    assert not session.browser_mode_enabled


def test_model_filter_does_not_mutate_original_request_and_disappears_when_off():
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True))
    tools = [{"name": "execute"}, {"name": "web_search"}, {"name": "obscura_browser_click"}]
    original = ModelRequest(
        model=Model(responses=[AIMessage("ok")]),
        tools=tools,
        messages=[HumanMessage("research")],
        system_message=SystemMessage("Base instructions"),
        state={},
    )
    try:
        prepared = BrowserModeMiddleware._prepare(original)
        assert [t["name"] for t in prepared.tools] == ["web_search", "obscura_browser_click"]
        assert BROWSER_PROMPT in prepared.system_message.text
        assert original.tools == tools
        assert original.system_message.text == "Base instructions"
        active_plan_session.get().browser_mode_enabled = False
        assert BrowserModeMiddleware._prepare(original) is original
    finally:
        active_plan_session.reset(token)


def test_discovery_excludes_coding_tools_and_subagents_even_after_prior_loads():
    discovery = ToolSearchMiddleware(deferred_subagents={"coder": "Build software"})
    tools = [{"name": "execute"}, {"name": "web_search"}, {"name": "obscura_browser_navigate"}]
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True))
    try:
        assert {entry["name"] for entry in discovery._catalog_for(tools)} == {
            "web_search",
            "obscura_browser_navigate",
        }
        active_plan_session.get().browser_mode_enabled = False
        assert {entry["name"] for entry in discovery._catalog_for(tools)} == {
            "execute",
            "web_search",
            "obscura_browser_navigate",
            "coder",
        }
    finally:
        active_plan_session.reset(token)


async def test_shared_loop_isolates_concurrent_browser_and_normal_turns():
    calls = []

    @tool("execute")
    async def execute() -> str:
        """Record a synthetic coding side effect."""
        await asyncio.sleep(0)
        calls.append("executed")
        return "done"

    async def run(name: str, *, enabled: bool) -> None:
        graph = create_agent(
            Model(responses=[call("execute", name), AIMessage("finished")]),
            tools=[execute],
            middleware=[BrowserModeMiddleware()],
        )
        session = state(name, enabled=False)
        session.browser_mode_enabled = enabled
        session.auto_approve = True
        _ = [event async for event in iterate_agent_events("Do the task", graph, "nova", session)]
        assert active_plan_session.get() is None

    await asyncio.gather(run("browser", enabled=True), run("normal", enabled=False))
    assert calls == ["executed"]


async def test_same_graph_keeps_browser_observations_for_normal_mode():
    @tool("obscura_browser_snapshot")
    def snapshot() -> str:
        """Read a synthetic browser observation."""
        return "Verified title at https://example.test: Browser evidence"

    @tool("execute")
    def execute() -> str:
        """Return a synthetic normal-mode result."""
        return "implementation done"

    model = RecordingModel(
        responses=[
            call(snapshot.name, "browse"),
            AIMessage("Findings: https://example.test"),
            call(execute.name, "implement"),
            AIMessage("Implemented using browser findings"),
        ]
    )
    graph = create_agent(
        model,
        tools=[snapshot, execute],
        middleware=[BrowserModeMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = SimpleNamespace(browser_mode_enabled=True)
    token = active_plan_session.set(session)
    config = {"configurable": {"thread_id": "shared-browser-conversation"}}
    try:
        first = await graph.ainvoke({"messages": [HumanMessage("Research the site")]}, config)
        assert "execute" not in model.bound[0]
        session.browser_mode_enabled = False
        second = await graph.ainvoke({"messages": [HumanMessage("Now implement it")]}, config)
        assert "execute" in model.bound[-1]
        observations = [m for m in second["messages"] if isinstance(m, ToolMessage)]
        assert any("Browser evidence" in m.content for m in observations)
        assert any("implementation done" in m.content for m in observations)
        assert second["messages"][: len(first["messages"])] == first["messages"]
    finally:
        active_plan_session.reset(token)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, ("toggle", "")),
        ("off", ("off", "")),
        ("on", ("on", "")),
        ("--status", ("status", "")),
        ("Research https://example.test", ("on", "Research https://example.test")),
    ],
)
def test_command_modes_and_task_text(text: str | None, expected: tuple[str, str]):
    assert browser_mode_args(text) == expected


async def test_non_tui_command_uses_current_agent_and_keeps_state_on_status():
    from novacode_cli.commands.browser_use_handler import handle_browser_use_command

    session = SessionState()
    session.plan_mode_enabled = True
    execute = AsyncMock()
    agent, tracker = object(), object()
    await handle_browser_use_command(
        agent,
        session,
        "nova",
        tracker,
        "Research Obscura",
        execute_fn=execute,
    )
    assert session.browser_mode_enabled
    assert not session.plan_mode_enabled
    execute.assert_awaited_once_with(
        user_input="Research Obscura",
        agent=agent,
        assistant_id="nova",
        session_state=session,
        token_tracker=tracker,
    )
    await handle_browser_use_command(agent, session, "nova", tracker, "status", execute_fn=execute)
    assert session.browser_mode_enabled
    await handle_browser_use_command(agent, session, "nova", tracker, "off", execute_fn=execute)
    assert not session.browser_mode_enabled
    assert execute.await_count == 1


async def test_allowed_browser_action_still_waits_for_approval_and_respects_rejection():
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langgraph.types import Command

    calls = []

    @tool("obscura_browser_click")
    def click() -> str:
        """Record a synthetic browser action needing approval."""
        calls.append("clicked")
        return "clicked"

    graph = create_agent(
        Model(responses=[call(click.name, "click"), AIMessage("Rejected; nothing clicked")]),
        tools=[click],
        middleware=[
            BrowserModeMiddleware(),
            HumanInTheLoopMiddleware(interrupt_on={click.name: True}),
        ],
        checkpointer=InMemorySaver(),
    )
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True))
    config = {"configurable": {"thread_id": "browser-approval"}}
    try:
        paused = await graph.ainvoke({"messages": [HumanMessage("Inspect the site")]}, config)
        assert paused["__interrupt__"]
        assert not calls
        finished = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "reject", "message": "Do not click"}]}), config
        )
        assert not calls
        assert any(
            isinstance(message, ToolMessage) and message.status == "error"
            for message in finished["messages"]
        )
    finally:
        active_plan_session.reset(token)


async def test_hard_denials_remain_authoritative_for_browser_tools(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.security import delegated_approval

    calls = []

    @tool("obscura_browser_navigate")
    def navigate() -> str:
        """Record a synthetic page navigation."""
        calls.append("navigated")
        return "opened"

    monkeypatch.setattr(
        delegated_approval,
        "get_policy",
        lambda: SimpleNamespace(evaluate=lambda *_args: SimpleNamespace(tier="deny")),
    )
    graph = create_agent(
        Model(responses=[call(navigate.name, "nav"), AIMessage("Denied")]),
        tools=[navigate],
        middleware=[BrowserModeMiddleware(), delegated_approval.HardDenyMiddleware()],
    )
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True, auto_approve=True))
    try:
        result = await graph.ainvoke({"messages": [HumanMessage("Open the page")]})
        assert not calls
        assert any(
            isinstance(message, ToolMessage) and message.status == "error"
            for message in result["messages"]
        )
    finally:
        active_plan_session.reset(token)
