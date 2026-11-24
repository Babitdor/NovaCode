"""Discovery must not block startup; retries must keep healthy servers usable."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from novacode_cli.mcp import middleware as module
from novacode_cli.mcp.client import build_mcp_config_dict
from novacode_cli.mcp.config import MCPConfig


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


def test_startup_provisioning_preserves_overrides_and_invalid_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.mcp.config.shutil.which", lambda name: name if name == "uvx" else None
    )
    path = tmp_path / "mcp.json"
    config = MCPConfig(path)
    saved_serena = {"transport": "stdio", "command": "custom", "disabled": True}
    path.write_text(json.dumps({"mcpServers": {"serena": saved_serena}}), encoding="utf-8")
    config.provision_startup_servers()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["mcpServers"]["serena"] == saved_serena
    assert config.get_server("browser-use").tools == ["browser_screenshot"]
    assert set(build_mcp_config_dict(config)) == {"browser-use"}

    path.write_text("{invalid", encoding="utf-8")
    config.provision_startup_servers()
    assert path.read_text(encoding="utf-8") == "{invalid"


async def test_default_servers_reconnect_from_warm_cache_and_unlock_agents(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from novacode_cli.agents.profiles import profile_readiness, profile_tool_allowed

    monkeypatch.setattr("novacode_cli.mcp.config.shutil.which", lambda name: name)
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig",
        lambda: SimpleNamespace(
            get=lambda key, default=None: default,
            get_browser_mode_servers=lambda: ["obscura"],
        ),
    )
    middleware = module.MCPMiddleware(tmp_path / "mcp.json")
    middleware.mcp_config.provision_startup_servers()
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", lambda: middleware)
    raw = {
        "serena": [Tool(name="find_symbol", inputSchema={"type": "object"})],
        "browser-use": [
            Tool(name=name, inputSchema={"type": "object"})
            for name in ("browser_screenshot", "browser_exec")
        ],
    }
    cache = {
        name: {
            "fingerprint": module._connection_fingerprint(connection),
            "tools": [tool.model_dump(mode="json") for tool in raw[name]],
        }
        for name, connection in build_mcp_config_dict(middleware.mcp_config).items()
    }
    monkeypatch.setattr(module, "_load_schema_cache", lambda: cache)
    monkeypatch.setattr(module, "_save_schema_cache", lambda _: None)
    connect = AsyncMock(side_effect=lambda name, _: SimpleNamespace(tools=raw[name]))
    monkeypatch.setattr(middleware._session_pool, "get", connect)
    monkeypatch.setattr(
        "langchain_mcp_adapters.tools._list_all_tools",
        AsyncMock(side_effect=lambda session: session.tools),
    )

    middleware.prepare_cached_tools()
    assert profile_readiness("nora")
    assert profile_readiness("serena")
    await middleware.start_background_discovery()
    assert connect.await_count == 2
    assert profile_readiness("nora") is None
    assert profile_readiness("serena") is None
    assert {tool.name for tool in middleware.tools} == {
        "serena_find_symbol",
        "browser-use_browser_screenshot",
    }
    screenshot = next(tool for tool in middleware.tools if tool.name.startswith("browser-use"))
    assert not profile_tool_allowed(
        "nora", screenshot.name, {**screenshot.metadata, "nova_builtin_browser_use": False}
    )


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
    scan = Mock(side_effect=AssertionError("status polling must not scan configuration"))
    monkeypatch.setattr(middleware.mcp_config, "list_servers", scan)
    app = SimpleNamespace(_mcp_failure_notices=set(), notify=Mock())
    for _ in range(100):
        NovaApp._poll_mcp_connections(app)
    scan.assert_not_called()
    app.notify.assert_called_once()
    assert "/mcp" in app.notify.call_args.args[0]


async def test_discovery_config_scan_leaves_event_loop_responsive(middleware, monkeypatch):
    import threading

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    scans = []

    def slow_scan():
        scans.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        finished.set()
        return {}

    monkeypatch.setattr(middleware.mcp_config, "list_servers", slow_scan)
    task = middleware.start_background_discovery()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not finished.is_set(), "configuration scan blocked input processing"
        assert scans[0] != threading.get_ident()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
        await middleware.aclose()
    assert len(scans) == 1


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
