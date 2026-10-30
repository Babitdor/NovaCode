"""Trusted browser server selection grants reviewed actions, never arbitrary MCP tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.agents.browser_mode import BrowserModeMiddleware
from novacode_cli.agents.tool_search import ToolSearchMiddleware
from novacode_cli.config.browser_mode import browser_tool_allowed
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.config.plan_mode import active_plan_session
from tests.test_prime import Model
from tests.test_skills_runtime import RecordingModel
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401


def _definition(server: str, raw: str) -> dict:
    return {
        "name": f"{server}_{raw}",
        "metadata": {"nova_mcp_server": server, "nova_mcp_tool": raw},
    }


def test_selected_server_actions_appear_in_models_and_discovery():
    NovaConfig().set("browser_mode_servers", ["my-playwright", "devtools"])
    tools = [
        _definition("my-playwright", "browser_navigate"),
        _definition("devtools", "take_snapshot"),
        _definition("other", "browser_navigate"),
        _definition("my-playwright", "browser_run_code"),
        _definition("devtools", "execute"),
    ]
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True))
    try:
        request = ModelRequest(
            model=Model(responses=[AIMessage("ok")]),
            tools=tools,
            messages=[HumanMessage("Research")],
            system_message=SystemMessage("Base"),
            state={},
        )
        expected = {"my-playwright_browser_navigate", "devtools_take_snapshot"}
        assert {t["name"] for t in BrowserModeMiddleware._prepare(request).tools} == expected
        assert {r["name"] for r in ToolSearchMiddleware()._catalog_for(tools)} == expected
    finally:
        active_plan_session.reset(token)


async def test_explicit_selection_is_required_at_execution_time():
    NovaConfig().set("browser_mode_servers", ["my-playwright"])
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True))
    tool = _definition("my-playwright", "browser_snapshot")
    request = ToolCallRequest(
        tool_call={"name": tool["name"], "id": "browse", "args": {}},
        tool=SimpleNamespace(metadata=tool["metadata"]),
        runtime=None,
        state={},
    )
    handler = AsyncMock(return_value="observed")
    try:
        assert await BrowserModeMiddleware().awrap_tool_call(request, handler) == "observed"
        NovaConfig().set("browser_mode_servers", [])
        result = await BrowserModeMiddleware().awrap_tool_call(request, handler)
        assert result.status == "error"
        assert handler.await_count == 1
    finally:
        active_plan_session.reset(token)


@pytest.mark.parametrize(
    "raw",
    [
        "execute",
        "read_file",
        "write_file",
        "MCPCode",
        "MCPTool",
        "task",
        "browser_run_code",
        "browser_install",
        "browser_file_upload",
        "unknown_action",
    ],
)
def test_trusting_a_server_does_not_trust_its_non_browser_tools(raw: str):
    definition = _definition("selected", raw)
    assert not browser_tool_allowed(
        definition["name"],
        definition["metadata"],
        trusted_servers=frozenset({"selected"}),
    )


def test_missing_or_spoofed_provenance_cannot_grant_other_browser_tools():
    trusted = frozenset({"selected"})
    assert not browser_tool_allowed("selected_browser_snapshot", trusted_servers=trusted)
    assert not browser_tool_allowed(
        "other_browser_snapshot",
        _definition("selected", "browser_snapshot")["metadata"],
        trusted_servers=trusted,
    )
    assert not browser_tool_allowed(
        "selected_browser_snapshot",
        {"nova_mcp_server": "selected"},
        trusted_servers=trusted,
    )
    assert not browser_tool_allowed(
        "web_search",
        {"nova_mcp_server": "unselected", "nova_mcp_tool": "web_search"},
        trusted_servers=trusted,
    )


@pytest.mark.parametrize("value", [None, "obscura", True, ["*"], ["obscura", 42], [" x"], ["x\ny"]])
def test_malformed_saved_selection_fails_closed(value: object):
    config = NovaConfig()
    config._config["browser_mode_servers"] = value  # A malformed hand-edited JSON value.
    assert config.get_browser_mode_servers() == []


def test_config_defaults_empty_selection_and_roundtrip():
    config = NovaConfig()
    assert config.get_browser_mode_servers() == ["obscura"]
    config.set_browser_mode_servers(["my-playwright", "devtools", "my-playwright"])
    assert NovaConfig().get_browser_mode_servers() == ["my-playwright", "devtools"]
    config.set_browser_mode_servers([])
    assert NovaConfig().get_browser_mode_servers() == []
    assert not browser_tool_allowed("obscura_browser_snapshot")
    with pytest.raises(ValueError, match="exact"):
        config.set_browser_mode_servers(["*"])
    assert NovaConfig().get_browser_mode_servers() == []


async def test_real_graph_discovery_and_revocation_with_previously_loaded_schemas(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("novacode_cli.skills.retrieval._model", lambda: None)
    NovaConfig().set_browser_mode_servers(["alias"])
    calls = []

    @tool("alias_browser_snapshot")
    def snapshot() -> str:
        """Read the selected browser's current page."""
        calls.append("read")
        return "Browser finding"

    snapshot.metadata = _definition("alias", "browser_snapshot")["metadata"]

    def call(name: str, identity: str, args: dict) -> AIMessage:
        return AIMessage("", tool_calls=[{"name": name, "args": args, "id": identity}])

    model = RecordingModel(
        responses=[
            call("search_tools", "search", {"query": "*"}),
            call(snapshot.name, "first", {}),
            AIMessage("Findings recorded"),
            call(snapshot.name, "revoked", {}),
            AIMessage("No access"),
        ]
    )
    discovery = ToolSearchMiddleware()
    discovery._frequent = set()
    graph = create_agent(
        model,
        tools=[snapshot],
        middleware=[BrowserModeMiddleware(), discovery],
        checkpointer=InMemorySaver(),
    )
    token = active_plan_session.set(SimpleNamespace(browser_mode_enabled=True))
    config = {"configurable": {"thread_id": "browser-revocation"}}
    try:
        await graph.ainvoke({"messages": [HumanMessage("Read the site")]}, config)
        assert calls == ["read"]
        assert snapshot.name in model.bound[1]
        NovaConfig().set_browser_mode_servers([])
        result = await graph.ainvoke({"messages": [HumanMessage("Read it again")]}, config)
        assert calls == ["read"]
        assert snapshot.name not in model.bound[-1]
        revoked = next(
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage) and message.tool_call_id == "revoked"
        )
        assert revoked.status == "error"
    finally:
        active_plan_session.reset(token)
