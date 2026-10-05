"""MCP tools in a background (async) agent graph.

A background graph resolves its declared tool names against ``novacode_cli.tools``
and, since this change, against the user's MCP config. Before that, every
``serena_*`` / ``playwright_*`` / ``cua-driver_*`` name an agent declared was
dropped silently, so an agent whose prompt is written around symbol navigation
ran in the background with no symbol tool at all.

The failure-mode tests are the load-bearing ones. ``langgraph`` loads every graph
in the config at startup and refuses to start if one fails to load, so a missing
or broken MCP server must degrade to "this agent has fewer tools" and never to
"no background agent can run". That is asserted directly rather than left to a
manual server run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novacode_cli.agents.async_agents import _mcp_tools


class _FakeTool:
    """A stand-in for a discovered MCP tool: only ``name`` is ever read."""

    def __init__(self, name: str) -> None:
        self.name = name


def _real_tool(name: str):
    """A genuine ``BaseTool``, for the tests that build a graph.

    ``create_deep_agent`` validates its tools, so the graph-level tests cannot
    use :class:`_FakeTool`; the name is what the grant matches on either way.
    """
    from langchain_core.tools import tool

    @tool(name)
    def _probe(query: str) -> str:
        """A probe tool."""
        return query

    return _probe


class _FakeMiddleware:
    """A middleware whose discovery yields *tools*, or raises if *boom*."""

    def __init__(self, tools: list[_FakeTool], *, boom: bool = False) -> None:
        self.tools = tools
        self._boom = boom
        self.discovered = 0

    def _discover_tools_sync(self) -> None:
        self.discovered += 1
        if self._boom:
            raise RuntimeError("mcp server refused to start")


@pytest.fixture(autouse=True)
def _clean_cache():
    """The cache is process-global; a stale entry would hide a real failure."""
    _mcp_tools.reset_cache()
    yield
    _mcp_tools.reset_cache()


def _install(monkeypatch, middleware: _FakeMiddleware) -> None:
    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", lambda: middleware)


# ── the grant ───────────────────────────────────────────────────────────────


def test_only_the_requested_mcp_tools_are_kept(monkeypatch):
    """An agent gets the servers it named, not every server the user configured."""
    _install(
        monkeypatch,
        _FakeMiddleware(
            [
                _FakeTool("serena_find_symbol"),
                _FakeTool("serena_read_file"),
                _FakeTool("playwright_browser_click"),
            ]
        ),
    )
    kept = _mcp_tools.mcp_tools_for(["serena_find_symbol", "code_search"])
    assert [t.name for t in kept] == ["serena_find_symbol"]


def test_a_nova_tool_name_is_not_an_mcp_tool(monkeypatch):
    """``code_search`` lives in novacode_cli.tools; the caller resolves it there.

    It must not be double-granted here, or the graph would carry two tools with
    the same name.
    """
    _install(monkeypatch, _FakeMiddleware([_FakeTool("serena_find_symbol")]))
    assert _mcp_tools.mcp_tools_for(["code_search", "docs_search"]) == []


def test_an_empty_request_does_not_discover(monkeypatch):
    """An agent with no ``tools:`` key must not spawn MCP servers at import."""
    middleware = _FakeMiddleware([_FakeTool("serena_find_symbol")])
    _install(monkeypatch, middleware)
    assert _mcp_tools.mcp_tools_for([]) == []
    assert middleware.discovered == 0


def test_a_name_no_server_provides_yields_nothing(monkeypatch):
    """A typo in the frontmatter is a missing tool, not an exception."""
    _install(monkeypatch, _FakeMiddleware([_FakeTool("serena_find_symbol")]))
    assert _mcp_tools.mcp_tools_for(["serena_does_not_exist"]) == []


# ── failure modes: never fatal ──────────────────────────────────────────────


def test_a_broken_mcp_server_yields_no_tools_rather_than_raising(monkeypatch):
    """The whole point: one bad server must not stop the graph from building.

    A raise here would propagate out of the graph module, and langgraph refuses
    to start the server if any registered graph fails to load — so every
    background agent would go quiet, not just this one.
    """
    _install(monkeypatch, _FakeMiddleware([], boom=True))
    assert _mcp_tools.mcp_tools_for(["serena_find_symbol"]) == []


def test_an_unimportable_mcp_stack_yields_no_tools(monkeypatch):
    """MCP support is optional; a background agent still runs without it."""

    def _explode():
        raise ImportError("no mcp package installed")

    monkeypatch.setattr("novacode_cli.mcp.get_shared_mcp_middleware", _explode)
    assert _mcp_tools.mcp_tools_for(["serena_find_symbol"]) == []


def test_a_tool_without_a_name_is_not_a_match(monkeypatch):
    """``name`` is what the request is expressed in, so a nameless tool is skipped."""
    _install(monkeypatch, _FakeMiddleware([_FakeTool(""), _FakeTool("serena_find_symbol")]))
    kept = _mcp_tools.mcp_tools_for(["serena_find_symbol"])
    assert [t.name for t in kept] == ["serena_find_symbol"]


# ── caching ─────────────────────────────────────────────────────────────────


def test_discovery_runs_once_per_distinct_request(monkeypatch):
    """The server imports every graph at startup; each must not re-spawn servers."""
    middleware = _FakeMiddleware([_FakeTool("serena_find_symbol")])
    _install(monkeypatch, middleware)

    _mcp_tools.mcp_tools_for(["serena_find_symbol"])
    _mcp_tools.mcp_tools_for(["serena_find_symbol"])
    assert middleware.discovered == 1

    # A different request is a different key, so it discovers on its own.
    _mcp_tools.mcp_tools_for(["serena_read_file"])
    assert middleware.discovered == 2


def test_the_cache_key_ignores_order_and_duplicates(monkeypatch):
    """Two agents naming the same tools in a different order share one discovery."""
    middleware = _FakeMiddleware([_FakeTool("serena_find_symbol")])
    _install(monkeypatch, middleware)

    _mcp_tools.mcp_tools_for(["serena_find_symbol", "serena_read_file"])
    _mcp_tools.mcp_tools_for(["serena_read_file", "serena_find_symbol", "serena_read_file"])
    assert middleware.discovered == 1


# ── the graph actually carries them ─────────────────────────────────────────


def test_build_specialist_graph_includes_the_mcp_tools(monkeypatch):
    """End of the chain: a declared MCP name reaches the built graph's tool node."""
    _install(monkeypatch, _FakeMiddleware([_real_tool("serena_find_symbol")]))
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "ollama")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "llama3")

    from novacode_cli.agents.async_agents._specialist import build_specialist_graph

    graph = build_specialist_graph(
        "probe-agent",
        {"prompt": "You explore code.", "tools": ["serena_find_symbol", "code_search"]},
    )
    tools_node = graph.get_graph().nodes["tools"]
    by_name = tools_node.data.tools_by_name
    assert "serena_find_symbol" in by_name, sorted(by_name)
    # The Nova tool is still resolved the old way, alongside it.
    assert "code_search" in by_name, sorted(by_name)


def test_build_specialist_graph_survives_a_broken_mcp_server(monkeypatch):
    """The graph still builds, so the server still starts."""
    _install(monkeypatch, _FakeMiddleware([], boom=True))
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "ollama")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "llama3")

    from novacode_cli.agents.async_agents._specialist import build_specialist_graph

    graph = build_specialist_graph(
        "probe-agent",
        {"prompt": "You explore code.", "tools": ["serena_find_symbol"]},
    )
    by_name = graph.get_graph().nodes["tools"].data.tools_by_name
    assert "serena_find_symbol" not in by_name
    # The file tools from the backend are still there, so the agent is usable.
    assert "read_file" in by_name, sorted(by_name)


# ── the user's own agents ───────────────────────────────────────────────────


def test_every_user_agent_marked_async_is_collected():
    """The four agents this change flagged, plus Serena, are all served.

    Skipped when the user's agent directory is absent, so the suite stays
    hermetic on a machine that has never created an agent.
    """
    from novacode_cli.agents.user_async_agents import collect_user_async_agents

    agents_root = Path.home() / ".nova" / "agents"
    if not agents_root.is_dir():
        pytest.skip("no user agent directory on this machine")

    collected = {graph_id for graph_id, _md in collect_user_async_agents()}
    expected = {
        "code-explorer",
        "code-reviewer",
        "code-simplier",
        "serena",
        "ultra-mode-agent",
    }
    missing = expected - collected
    assert not missing, f"not collected as async: {sorted(missing)}"
