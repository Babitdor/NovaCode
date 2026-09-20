"""Stateful MCP servers keep one session across tool calls.

Tools used to be built with ``session=None``, which makes
``langchain_mcp_adapters`` open a fresh session per call — for a stdio server,
respawning the server process and discarding everything the previous call did.
Measured against the real Playwright MCP before the fix: ``browser_navigate``
reached example.com and the very next ``browser_snapshot`` reported
``about:blank``. After it, the snapshot stays on example.com and returns in
0.0s instead of 2.9s because the browser is no longer relaunched.

These use a fake session factory — the property under test is "how many
sessions get created", which needs no browser to observe.
"""

from __future__ import annotations

import asyncio

import pytest

from novacode_cli.mcp import middleware as mw


class _FakeSession:
    """Stands in for an MCP ClientSession, remembering its own state."""

    def __init__(self, ident: int) -> None:
        self.ident = ident
        self.initialized = False
        self.calls: list[tuple[str, dict]] = []
        self.page = "about:blank"  # the state a per-call session would lose

    async def initialize(self) -> None:
        self.initialized = True

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, dict(arguments or {})))
        if name == "navigate":
            self.page = (arguments or {}).get("url", "")
        if name == "boom":
            msg = "transport died"
            raise RuntimeError(msg)
        return f"session={self.ident} page={self.page}"


class _Factory:
    """Counts how many sessions get created, and can fail on demand."""

    def __init__(self, fail_times: int = 0) -> None:
        self.created: list[_FakeSession] = []
        self.fail_times = fail_times
        self.closed = 0

    def __call__(self, connection, **kwargs):
        factory = self

        class _Ctx:
            async def __aenter__(self):
                if factory.fail_times > 0:
                    factory.fail_times -= 1
                    msg = "server failed to start"
                    raise RuntimeError(msg)
                session = _FakeSession(len(factory.created) + 1)
                factory.created.append(session)
                return session

            async def __aexit__(self, *exc):
                factory.closed += 1
                return False

        return _Ctx()


@pytest.fixture
def factory(monkeypatch):
    made = _Factory()
    import langchain_mcp_adapters.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "create_session", made)
    return made


STDIO = {"transport": "stdio", "command": "x", "args": []}


# ── The bug ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_calls_share_one_session(factory):
    """The whole point: a per-call session is what broke stateful servers."""
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "playwright", STDIO)

    for _ in range(5):
        await proxy.call_tool("snapshot", {})

    assert len(factory.created) == 1, (
        f"opened {len(factory.created)} sessions for 5 calls — state cannot survive"
    )
    await pool.aclose()


@pytest.mark.asyncio
async def test_state_written_by_one_call_is_visible_to_the_next(factory):
    """navigate -> snapshot, the exact sequence that returned about:blank."""
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "playwright", STDIO)

    await proxy.call_tool("navigate", {"url": "https://example.com"})
    out = await proxy.call_tool("snapshot", {})

    assert "page=https://example.com" in out, f"state was lost between calls: {out}"
    await pool.aclose()


@pytest.mark.asyncio
async def test_the_session_is_initialized_exactly_once(factory):
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "s", STDIO)
    await proxy.call_tool("a", {})
    await proxy.call_tool("b", {})
    assert factory.created[0].initialized
    assert len(factory.created) == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_each_server_gets_its_own_session(factory):
    pool = mw._PersistentSessionPool()
    a = mw._SessionProxy(pool, "playwright", STDIO)
    b = mw._SessionProxy(pool, "serena", STDIO)
    await a.call_tool("x", {})
    await b.call_tool("x", {})
    assert len(factory.created) == 2, "servers must not share a session"
    await pool.aclose()


@pytest.mark.asyncio
async def test_concurrent_first_calls_do_not_race_into_two_sessions(factory):
    """Two tool calls at once must not each spawn the server."""
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "s", STDIO)
    await asyncio.gather(*(proxy.call_tool("x", {}) for _ in range(6)))
    assert len(factory.created) == 1
    await pool.aclose()


# ── Recovery ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failing_call_drops_the_session_so_the_next_reconnects(factory):
    """A cached dead session would fail every later call too."""
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "s", STDIO)
    await proxy.call_tool("ok", {})
    with pytest.raises(RuntimeError):
        await proxy.call_tool("boom", {})
    await proxy.call_tool("ok", {})
    assert len(factory.created) == 2, "did not reconnect after a transport error"
    await pool.aclose()


@pytest.mark.asyncio
async def test_a_server_that_will_not_start_raises_and_leaves_no_wreckage(monkeypatch):
    made = _Factory(fail_times=1)
    import langchain_mcp_adapters.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "create_session", made)

    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "s", STDIO)
    with pytest.raises(Exception):  # noqa: B017 — any failure is acceptable here
        await proxy.call_tool("x", {})
    # The next attempt must be allowed to try again.
    out = await proxy.call_tool("x", {})
    assert "session=1" in out
    await pool.aclose()


@pytest.mark.asyncio
async def test_a_dead_owner_task_is_replaced(factory):
    """If the server process dies, the pool must notice and restart it."""
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "s", STDIO)
    await proxy.call_tool("x", {})

    owner = pool._owners["s"]
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)

    await proxy.call_tool("x", {})
    assert len(factory.created) == 2
    await pool.aclose()


@pytest.mark.asyncio
async def test_closing_the_pool_releases_the_sessions(factory):
    pool = mw._PersistentSessionPool()
    proxy = mw._SessionProxy(pool, "s", STDIO)
    await proxy.call_tool("x", {})
    await pool.aclose()
    assert pool._sessions == {}
    assert pool._owners == {}


# ── Which transports get a session ──────────────────────────────────────────


def test_only_stdio_tools_are_bound_to_a_persistent_session(monkeypatch):
    """Remote transports reconnect cheaply and stay on the proven path, so
    this change cannot regress them."""
    captured: list = []

    def _fake_convert(session, raw, **kwargs):
        captured.append(session)
        from langchain_core.tools import StructuredTool

        return StructuredTool.from_function(
            func=lambda: "x", name=f"{kwargs.get('server_name')}_{raw.name}",
            description="d",
        )

    import langchain_mcp_adapters.tools as tools_mod

    monkeypatch.setattr(tools_mod, "convert_mcp_tool_to_langchain_tool", _fake_convert)

    from mcp.types import Tool as MCPTool

    raw = [MCPTool(name="t", description="d", inputSchema={"type": "object"})]
    middleware = mw.MCPMiddleware()

    middleware._convert_raw_tools("playwright", dict(STDIO), raw)
    assert isinstance(captured[-1], mw._SessionProxy), "stdio got no persistent session"

    middleware._convert_raw_tools(
        "remote", {"transport": "streamable_http", "url": "http://x"}, raw
    )
    assert captured[-1] is None, "a remote server was pinned to a persistent session"
