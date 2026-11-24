"""Serena mode: MCP-only tool boundary, Serena prompt, session gating."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from novacode_cli.agents.serena_mode import (
    SERENA_MODE_PROMPT,
    SerenaModeMiddleware,
    serena_mcp_ready,
    serena_tool_allowed,
)
from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.states.Session import SessionState
from tests.test_prime import Model


def _mcp_tool(name: str, server: str = "serena") -> SimpleNamespace:
    return SimpleNamespace(name=name, metadata={"nova_mcp_server": server, "nova_mcp_tool": name})


@pytest.mark.parametrize(
    ("name", "metadata", "allowed"),
    [
        ("serena_find_symbol", {"nova_mcp_server": "serena", "nova_mcp_tool": "find_symbol"}, True),
        ("playwright_browser_navigate", {"nova_mcp_server": "playwright"}, True),
        ("MCPTool", None, True),
        ("eval", None, True),
        ("MCPCode", None, True),
        ("task", None, True),
        ("read_file", None, False),
        ("web_search", None, False),
        ("execute", None, False),
        ("write_todos", None, False),
        ("ask_user_question", None, False),
    ],
)
def test_serena_tool_allowed(name: str, metadata: dict | None, allowed: bool) -> None:
    assert serena_tool_allowed(name, metadata) is allowed


@pytest.mark.parametrize("name", ["read_file", "execute", "web_search", "unknown_tool"])
async def test_disallowed_calls_never_reach_execution(name: str):
    token = active_plan_session.set(SimpleNamespace(serena_mode_enabled=True, auto_approve=True))
    request = ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "blocked"},
        tool=None,
        state={},
        runtime=None,
    )
    from unittest.mock import AsyncMock, Mock

    sync, asynchronous = Mock(), AsyncMock()
    try:
        assert SerenaModeMiddleware().wrap_tool_call(request, sync).status == "error"
        assert (
            await SerenaModeMiddleware().awrap_tool_call(request, asynchronous)
        ).status == "error"
        sync.assert_not_called()
        asynchronous.assert_not_called()
    finally:
        active_plan_session.reset(token)


async def test_allowed_mcp_call_reaches_execution():
    from unittest.mock import AsyncMock

    token = active_plan_session.set(SimpleNamespace(serena_mode_enabled=True, auto_approve=True))
    tool = _mcp_tool("serena_find_symbol")
    request = ToolCallRequest(
        tool_call={"name": "serena_find_symbol", "args": {}, "id": "go"},
        tool=tool,
        state={},
        runtime=None,
    )
    downstream = AsyncMock(return_value="symbol data")
    try:
        assert await SerenaModeMiddleware().awrap_tool_call(request, downstream) == "symbol data"
        downstream.assert_awaited_once()
    finally:
        active_plan_session.reset(token)


def test_model_filter_strips_non_mcp_tools_and_appends_the_prompt():
    token = active_plan_session.set(SimpleNamespace(serena_mode_enabled=True))
    tools = [
        {"name": "read_file"},
        {"name": "web_search"},
        {"name": "serena_find_symbol", "metadata": {"nova_mcp_server": "serena"}},
        {"name": "MCPTool"},
        {"name": "eval"},
    ]
    original = ModelRequest(
        model=Model(responses=[AIMessage("ok")]),
        tools=tools,
        messages=[HumanMessage("explore the codebase")],
        system_message=SystemMessage("Base instructions"),
        state={},
    )
    try:
        prepared = SerenaModeMiddleware._prepare(original)
        assert [t["name"] for t in prepared.tools] == [
            "serena_find_symbol",
            "MCPTool",
            "eval",
        ]
        assert SERENA_MODE_PROMPT in prepared.system_message.text
        assert original.tools == tools, "the original request is never mutated"
        active_plan_session.get().serena_mode_enabled = False
        assert SerenaModeMiddleware._prepare(original) is original
    finally:
        active_plan_session.reset(token)


def test_mode_switches_are_mutually_exclusive():
    session = SessionState()
    session.serena_mode_enabled = True
    assert session.serena_mode_enabled
    assert not session.plan_mode_enabled and not session.browser_mode_enabled
    session.plan_mode_enabled = True
    assert not session.serena_mode_enabled
    session.serena_mode_enabled = True
    session.browser_mode_enabled = True
    assert not session.serena_mode_enabled
    session.reset_conversation()
    assert not session.serena_mode_enabled


def test_mcp_tools_are_bound_not_deferred_when_serena_is_on(monkeypatch):
    """ToolSearch must not hide the MCP toolset behind search_tools: in Serena
    mode the MCP inventory IS the toolset."""
    from novacode_cli.agents.tool_search import ToolSearchMiddleware

    discovery = ToolSearchMiddleware(deferred_subagents={"coder": "Build software"})
    tools = [
        SimpleNamespace(
            name="serena_find_symbol",
            metadata={"nova_mcp_server": "serena", "nova_mcp_tool": "find_symbol"},
        ),
        SimpleNamespace(name="MCPTool", metadata=None),
    ]
    original = ModelRequest(
        model=Model(responses=[AIMessage("ok")]),
        tools=tools,
        messages=[HumanMessage("explore")],
        system_message=SystemMessage("Base"),
        state={},
    )
    token = active_plan_session.set(SimpleNamespace(serena_mode_enabled=True))
    try:
        applied = discovery._apply(original)
        bound = {t.name for t in applied.tools}
        assert bound == {"serena_find_symbol", "MCPTool"}
        assert "## More tools" not in applied.system_message.text
    finally:
        active_plan_session.reset(token)


def test_serena_mcp_ready_reports_a_missing_server(monkeypatch) -> None:
    """Without MCP runtime state the gate refuses with guidance, never raises."""
    import novacode_cli.mcp as mcp_runtime

    def broken():
        raise RuntimeError("no runtime")

    monkeypatch.setattr(mcp_runtime, "get_shared_mcp_middleware", broken)
    ready, message = serena_mcp_ready()
    assert ready is False
    assert "Serena" in message
