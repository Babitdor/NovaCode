"""Discovery must not block startup; retries must keep healthy servers usable."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from novacode_cli.mcp import middleware as module


@pytest.fixture
def middleware(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {"transport": "stdio", "command": "python"},
                    "bad": {"transport": "stdio", "command": "python"},
                }
            }
        )
    )
    monkeypatch.setattr(module, "_load_schema_cache", lambda: {})
    monkeypatch.setattr(module, "_save_schema_cache", lambda _: None)
    return module.MCPMiddleware(config)


async def test_startup_is_nonblocking_and_discovery_is_shared(middleware, monkeypatch):
    release = asyncio.Event()
    started = asyncio.Event()

    async def fetch(connection):
        started.set()
        await release.wait()
        return [Tool(name="read", inputSchema={"type": "object"})]

    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(module, "_fetch_raw_tools", mock)
    middleware.prepare_cached_tools()
    assert mock.await_count == 0
    task = middleware.start_background_discovery()
    assert task is middleware.start_background_discovery()
    await asyncio.wait_for(started.wait(), 2)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 2)
    assert mock.await_count == 2
    assert set(middleware.server_status.values()) == {"ready"}


async def test_retry_bypasses_cache_and_preserves_other_tools(middleware, monkeypatch):
    fetch = AsyncMock(
        side_effect=[
            [Tool(name="read", inputSchema={"type": "object"})],
            RuntimeError("failed"),
            [Tool(name="write", inputSchema={"type": "object"})],
        ]
    )
    monkeypatch.setattr(module, "_fetch_raw_tools", fetch)
    await middleware.start_background_discovery()
    assert middleware.server_status["bad"] == "failed"
    assert "bad" in middleware.connection_errors
    good = next(t for t in middleware.tools if t.name == "good_read")
    assert await middleware.retry_server("bad")
    assert next(t for t in middleware.tools if t.name == "good_read") is good
    assert "bad" not in middleware.connection_errors
    assert {t.name for t in middleware.tools} == {"good_read", "bad_write"}


async def test_late_subagent_tools_are_callable_and_keep_grants(middleware):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.tools import tool

    from novacode_cli.mcp.access import MCPAccessMiddleware
    from tests.test_generic_mcp_tool import ScriptedModel

    @tool
    def good_read() -> str:
        """Read from the allowed server."""
        return "late tool works"

    @tool
    def bad_read() -> str:
        """Read from a server without a grant."""
        raise AssertionError("Ungrantable tool executed")

    middleware._tools_discovered = True
    access = MCPAccessMiddleware(middleware, ["good"], [])
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"id": "late", "name": "good_read", "args": {}}]),
                AIMessage(content="done"),
            ]
        )
    )
    agent = create_agent(model, tools=[], middleware=[access])
    middleware.tools.extend([good_read, bad_read])
    assert access._tools() == [good_read]
    result = await agent.ainvoke({"messages": [HumanMessage(content="read")]})
    assert any(
        isinstance(m, ToolMessage) and m.content == "late tool works" for m in result["messages"]
    )


async def test_mcp_retry_button_uses_selected_server(middleware, monkeypatch):
    from textual.app import App
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import McpScreen

    middleware._tools_discovered = True
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", lambda: middleware)
    monkeypatch.setattr("novacode_cli.mcp.config.MCPConfig", lambda: middleware.mcp_config)
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(middleware, "retry_server", retry)

    class ScreenApp(App):
        async def on_mount(self):
            await self.push_screen(McpScreen())

    async with ScreenApp().run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        pilot.app.screen.query_one("#mcp-configured", OptionList).highlighted = 1
        await pilot.click("#retry")
        await pilot.pause()
        retry.assert_awaited_once_with("bad")


def test_failure_notification_is_deduplicated(middleware, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from novacode_cli.tui.app import NovaApp

    middleware.connection_errors["bad"] = "TimeoutError"
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", lambda: middleware)
    monkeypatch.setattr("novacode_cli.mcp.config.MCPConfig", lambda: middleware.mcp_config)
    monkeypatch.setattr(
        middleware, "start_background_discovery", lambda: SimpleNamespace(done=lambda: False)
    )
    app = SimpleNamespace(_mcp_failure_notices=set(), notify=Mock())
    NovaApp._poll_mcp_connections(app)
    NovaApp._poll_mcp_connections(app)
    app.notify.assert_called_once()
    assert "/mcp" in app.notify.call_args.args[0]


async def test_close_cancels_discovery_and_closes_sessions(middleware, monkeypatch):
    started = asyncio.Event()

    async def fetch(connection):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_fetch_raw_tools", fetch)
    close = AsyncMock()
    monkeypatch.setattr(middleware._session_pool, "aclose", close)
    task = middleware.start_background_discovery()
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(middleware.aclose(), 2)
    assert task.cancelled()
    close.assert_awaited_once()


async def test_ordinary_turn_does_not_wait_for_mcp(middleware, monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage

    from novacode_cli.mcp.generic import build_mcp_tool
    from tests.test_generic_mcp_tool import ScriptedModel

    async def fetch(connection):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_fetch_raw_tools", fetch)
    model = ScriptedModel(messages=iter([AIMessage(content="ready")]))
    agent = create_agent(model, tools=[build_mcp_tool(middleware)], middleware=[middleware])
    try:
        result = await asyncio.wait_for(
            agent.ainvoke({"messages": [HumanMessage(content="hello")]}), 2
        )
        assert result["messages"][-1].content == "ready"
        assert not middleware._discovery_task.done()
    finally:
        await middleware.aclose()


async def test_malformed_cache_falls_back_to_background_discovery(middleware, monkeypatch):
    monkeypatch.setattr(module, "_load_schema_cache", lambda: {"good": ["invalid"]})
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "_fetch_raw_tools", fetch)
    middleware.prepare_cached_tools()
    assert fetch.await_count == 0
    await middleware.start_background_discovery()
    assert fetch.await_count == 2
    assert middleware.server_status == {"good": "ready", "bad": "ready"}


def test_skill_aliases_follow_background_inventory():
    from types import SimpleNamespace

    from novacode_cli.skills.tool_resolver import inventory_resolver

    tools, metadata = [], []
    resolve = inventory_resolver(tools, metadata)
    assert resolve("peer", None) == []
    target = SimpleNamespace(name="peer_read")
    tools.append(target)
    metadata.append({"name": "peer_read", "server": "peer", "skill_alias": "read"})
    assert resolve("peer", None) == [target]
    assert resolve("read", None) == [target]
