"""Middleware for integrating MCP servers with the agent.

This middleware loads MCP server configurations, discovers their tools,
and makes them available to the agent as callable functions.

Uses langchain-mcp-adapters for robust MCP client management with
persistent connections for stateful MCP servers.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from mcp.types import Tool as MCPTool

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from mcp.client.session import ClientSession

from novacode_cli.config.config import console
from novacode_cli.mcp.client import MultiServerMCPClient
from novacode_cli.mcp.config import MCPConfig
from novacode_cli.prompts import render_template

logger = logging.getLogger(__name__)


class _PersistentSessionPool:
    """One long-lived MCP session per stdio server, shared by every tool call.

    Without this, tools are built with ``session=None`` and
    ``langchain_mcp_adapters`` opens a *fresh* session per call — for a stdio
    server that means spawning the server process again and throwing away
    everything the last call did. Stateful servers are then broken in a way
    that looks intermittent: one-shot calls work, sequences never do.
    Measured against Playwright MCP: ``browser_navigate`` reached
    example.com, and the very next ``browser_snapshot`` reported
    ``about:blank`` because it was talking to a different browser.

    A session is owned by its own background task rather than by whichever
    tool call happened to open it. The stdio transport is an anyio task group
    holding the child's pipes: if it were entered inside a tool call, those
    pipes would close the moment that call returned, which is the very bug
    this exists to fix.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._owners: dict[str, asyncio.Task] = {}
        self._closers: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _lock_for(self, server: str) -> asyncio.Lock:
        lock = self._locks.get(server)
        if lock is None:
            lock = self._locks[server] = asyncio.Lock()
        return lock

    async def _own(
        self,
        server: str,
        connection: dict[str, Any],
        ready: asyncio.Future,
        closing: asyncio.Event,
    ) -> None:
        """Hold one session open until asked to close. Runs as its own task."""
        from langchain_mcp_adapters.sessions import create_session

        try:
            async with create_session(connection) as session:  # type: ignore[arg-type]
                await session.initialize()
                if not ready.done():
                    ready.set_result(session)
                await closing.wait()
        # BaseException, not Exception: a failing stdio spawn surfaces as an
        # anyio BaseExceptionGroup, and the waiter must hear about it rather
        # than block until timeout. Always re-raised.
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(
                    exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                )
            raise
        finally:
            self._sessions.pop(server, None)

    async def get(self, server: str, connection: dict[str, Any]) -> ClientSession:
        """Return the live session for *server*, starting one if needed."""
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            # A different event loop (a fresh TUI run, a test). Sessions are
            # bound to the loop that created them, so start over.
            await self.aclose(_from_dead_loop=True)
        self._loop = loop

        async with self._lock_for(server):
            session = self._sessions.get(server)
            owner = self._owners.get(server)
            if session is not None and owner is not None and not owner.done():
                return session
            # Dead or missing — drop the remains and start a fresh one.
            await self._stop(server)

            ready: asyncio.Future = loop.create_future()
            closing = asyncio.Event()
            task = loop.create_task(self._own(server, connection, ready, closing))
            self._owners[server] = task
            self._closers[server] = closing
            try:
                session = await ready
            except BaseException:
                await self._stop(server)
                raise
            self._sessions[server] = session
            return session

    async def _stop(self, server: str) -> None:
        """Tear one session down, tolerating a half-started one."""
        self._sessions.pop(server, None)
        closing = self._closers.pop(server, None)
        if closing is not None:
            closing.set()
        task = self._owners.pop(server, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def drop(self, server: str) -> None:
        """Discard a session so the next call reconnects.

        Called when a tool errors: the failure may have been the transport
        dying, and a cached dead session would fail every later call too.
        """
        async with self._lock_for(server):
            await self._stop(server)

    async def aclose(self, *, _from_dead_loop: bool = False) -> None:
        """Close every session (process shutdown, or a new event loop)."""
        for server in list(self._owners):
            if _from_dead_loop:
                # The owning loop is gone; awaiting its tasks would hang.
                self._sessions.pop(server, None)
                self._closers.pop(server, None)
                self._owners.pop(server, None)
            else:
                await self._stop(server)
        self._locks.clear()


class _SessionProxy:
    """What the adapter is handed instead of ``None``.

    ``convert_mcp_tool_to_langchain_tool`` only ever calls ``call_tool`` on the
    session it is given, so resolving the real one here defers connection to
    the first actual use — on the loop that will use it — rather than at
    discovery time.
    """

    def __init__(
        self, pool: _PersistentSessionPool, server: str, connection: dict[str, Any]
    ) -> None:
        self._pool = pool
        self._server = server
        self._connection = connection

    async def call_tool(self, name: str, arguments: Any, **kwargs: Any) -> Any:
        session = await self._pool.get(self._server, self._connection)
        try:
            return await session.call_tool(name, arguments, **kwargs)
        except BaseException:
            # The transport may have died with it; force a reconnect next time
            # rather than pinning every later call to a corpse.
            await self._pool.drop(self._server)
            raise


#: Cap on the structured-result text folded into a tool result.
_STRUCTURED_MAX_CHARS = 12_000


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _render_structured(structured: Any, text: str) -> str:
    """``structuredContent`` as compact text, minus what ``text`` already says.

    MCP servers may put their machine-readable result ONLY in
    ``structuredContent``, which langchain-mcp-adapters files as an artifact the
    model never sees. cua-driver does exactly that with the element tokens and
    ``snapshot_id`` its ``click`` requires: the model saw an indexed tree,
    could not click by element ("bare element_index is not accepted"), and fell
    back to guessing pixel coordinates from a screenshot caption.
    """
    if not isinstance(structured, dict):
        return _compact(structured)[:_STRUCTURED_MAX_CHARS]
    lines: list[str] = []
    for key, value in structured.items():
        if key.startswith("_") or value in (None, "", [], {}):
            continue  # private / deprecated fields, empties
        if isinstance(value, str) and value.strip() and value.strip() in text:
            continue  # already in the text part (e.g. tree_markdown)
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            lines.append(f"{key}:")
            lines += [
                "- " + " ".join(f"{k}={_compact(v)}" for k, v in item.items()) for item in value
            ]
        else:
            lines.append(f"{key}: {_compact(value)}")
    out = "\n".join(lines)
    if len(out) > _STRUCTURED_MAX_CHARS:
        out = out[:_STRUCTURED_MAX_CHARS] + "\n… (structured result truncated)"
    return out


def _with_structured(result: Any) -> Any:
    """Append an MCP result's structured part to the content the model sees."""
    if not (isinstance(result, tuple) and len(result) == 2):
        return result
    content, artifact = result
    structured = (artifact or {}).get("structured_content") if isinstance(artifact, dict) else None
    if structured is None:
        return result
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    else:
        return result
    extra = _render_structured(structured, text)
    if not extra:
        return result
    block = f"structured result:\n{extra}"
    if isinstance(content, str):
        return (f"{content}\n\n{block}" if content.strip() else block), artifact
    return [*content, {"type": "text", "text": block}], artifact



def _wrap_tool_error_handling(tool: BaseTool) -> BaseTool:
    """Make an MCP tool failure non-fatal to the agent run.

    MCP servers run over anyio task groups, so a tool-side failure (a
    server-side ``ERR_CONNECTION_REFUSED`` from playwright, a crashed server,
    a timeout) can surface as an exception — sometimes a
    ``BaseExceptionGroup`` — that escapes LangGraph's ToolNode error handling
    (which only catches ``Exception``) and aborts the entire turn.

    Wrapping the tool's coroutine/func to catch ``BaseException`` and return a
    readable error string means the model just sees the failure as the tool's
    result and can recover (retry, start the server, or report) instead of the
    run dying. ``KeyboardInterrupt``/``SystemExit`` are re-raised so Ctrl-C and
    shutdown still work.
    """

    # MCP tools are loaded with response_format="content_and_artifact", so they
    # must return a (content, artifact) two-tuple — returning a bare string for
    # those raises "a two-tuple ... is expected". Match the tool's declared
    # format so the error result is well-formed either way.
    response_format = getattr(tool, "response_format", "content")

    def _error_result(exc: BaseException) -> Any:
        # Plain ASCII (no emoji) so the result can't trip a cp1252 console.
        msg = f"[MCP error] tool '{tool.name}' failed: {exc}"
        if response_format == "content_and_artifact":
            return (msg, None)
        return msg

    orig_coroutine = getattr(tool, "coroutine", None)
    if orig_coroutine is not None:

        async def _safe_coroutine(*args: Any, **kwargs: Any) -> Any:
            try:
                return _with_structured(await orig_coroutine(*args, **kwargs))
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001
                logger.warning("MCP tool '%s' failed: %s", tool.name, exc)
                return _error_result(exc)

        try:
            tool.coroutine = _safe_coroutine
        except Exception:  # noqa: BLE001
            pass

    orig_func = getattr(tool, "func", None)
    if orig_func is not None:

        def _safe_func(*args: Any, **kwargs: Any) -> Any:
            try:
                return _with_structured(orig_func(*args, **kwargs))
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001
                logger.warning("MCP tool '%s' failed: %s", tool.name, exc)
                return _error_result(exc)

        try:
            tool.func = _safe_func
        except Exception:  # noqa: BLE001
            pass

    return tool


# ── MCP tool-schema disk cache ───────────────────────────────────────────────
# Discovering tools live means cold-spawning every stdio server (npx/uvx) just
# to list its tools — ~5-11s of agent-build time on every boot. Tool schemas
# almost never change, and the converted tools open a FRESH session per call
# anyway (stateless pattern), so a tool rebuilt from a cached schema behaves
# identically at call time. Boot therefore serves schemas from this cache and
# refreshes it in a background thread, so drift is at most one boot stale.
# Entries are keyed by server name and invalidated by a connection-config hash.
_SCHEMA_CACHE_PATH = Path.home() / ".nova" / "mcp_tools_cache.json"

# Only refresh entries older than this. Refreshing on every boot would spawn
# every cached stdio server in the background each session — wasted CPU/RAM
# and their startup logs would spew mid-session. Daily is plenty for schemas.
_SCHEMA_REFRESH_TTL_SECONDS = 24 * 3600.0


def _connection_fingerprint(connection: dict[str, Any]) -> str:
    """Hash of a server's connection config — cache invalidation key."""
    return hashlib.sha256(
        json.dumps(connection, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _load_schema_cache() -> dict[str, Any]:
    try:
        return json.loads(_SCHEMA_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt cache just means live discovery
        return {}


def _save_schema_cache(entries: dict[str, Any]) -> None:
    try:
        _SCHEMA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SCHEMA_CACHE_PATH.write_text(json.dumps(entries), encoding="utf-8")
    except Exception:  # noqa: BLE001 - cache write failure must never break discovery
        logger.debug("Could not write MCP schema cache", exc_info=True)


async def _fetch_raw_tools(connection: dict[str, Any]) -> list[MCPTool]:
    """Connect to one server and list its raw MCP tool schemas."""
    from langchain_mcp_adapters.sessions import create_session
    from langchain_mcp_adapters.tools import _list_all_tools

    async with create_session(connection) as session:  # type: ignore[arg-type]
        await session.initialize()
        return await _list_all_tools(session)


class MCPState(AgentState):
    """State for the MCP middleware."""

    mcp_tools: NotRequired[list[dict[str, Any]]]  # type: ignore[misc]
    """List of MCP tools metadata (name, description, server)."""


class MCPStateUpdate(TypedDict):
    """State update for the MCP middleware."""

    mcp_tools: list[dict[str, Any]]
    """List of MCP tools metadata."""


# MCP system prompt loaded from: NovaCode_cli/prompts/mcp.jinja


class MCPMiddleware(AgentMiddleware):
    """Middleware for integrating MCP servers with the agent.

    This middleware:
    - Loads MCP server configurations from ~/.nova/mcp.json
    - Discovers tools from configured MCP servers using langchain-mcp-adapters
    - Maintains persistent sessions for stateful MCP servers
    - Registers MCP tools with the agent

    Args:
        config_path: Optional path to mcp.json config file
    """

    state_schema = MCPState

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialize the MCP middleware.

        Uses lazy discovery - tools are discovered on first use instead of at init time.
        This significantly speeds up agent startup by deferring MCP server connections.

        Args:
            config_path: Optional path to mcp.json config file.
                       Defaults to ~/.nova/mcp.json
        """
        self.mcp_config = MCPConfig(config_path)
        self._client: MultiServerMCPClient | None = None
        self._tools_cache: list[dict[str, Any]] = []
        self.tools: list[BaseTool] = []
        # One reused session per stdio server. Stateful servers (a browser, an
        # indexed project) are unusable without this — see
        # _PersistentSessionPool.
        self._session_pool = _PersistentSessionPool()

        # Lazy discovery - tools are discovered on first use
        self._tools_discovered = False
        self._discovery_lock = asyncio.Lock()
        # Cache for rendered MCP section to avoid re-rendering on every request
        self._mcp_section_cache: str | None = None
        self._mcp_section_cache_time: float = 0
        self._mcp_section_cache_ttl: float = 30.0  # 30 seconds TTL

    async def _ensure_tools_discovered(self) -> None:
        """Ensure tools are discovered before first use.

        This lazy discovery pattern defers MCP server connections until
        the first tool call, significantly speeding up agent startup.
        """
        if self._tools_discovered:
            return

        async with self._discovery_lock:
            if self._tools_discovered:
                return
            await self._discover_tools_async()
            self._tools_discovered = True

    def _discover_tools_sync(self) -> None:
        """Discover tools synchronously by running async discovery in a dedicated thread.

        Uses ``concurrent.futures.ThreadPoolExecutor`` to run ``asyncio.run()`` in a
        *separate* thread with its own event loop, avoiding the need for
        ``nest_asyncio`` and preventing blocking the main event loop.

        This is safe to call from:
        - A sync CLI command handler (no running loop)
        - A Textual worker thread (``@work(thread=True)``)
        - Any context where a loop *is* running (the dedicated thread handles it)
        """
        servers = self.mcp_config.list_servers()

        if not servers:
            return

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, self._discover_tools_async())
            # 90-second blanket timeout — discovery involves network I/O and
            # cold process starts for stdio servers (e.g. ``npx``/``uvx`` may
            # download packages on first run). A short timeout abandons the
            # result while the worker is still spawning servers, leaving
            # ``self.tools`` empty so MCP tools never register.
            future.result(timeout=90)

    def _convert_raw_tools(
        self,
        server_name: str,
        connection: dict[str, Any],
        raw_tools: list[MCPTool],
    ) -> list[BaseTool]:
        """Convert raw MCP tool schemas to wrapped LangChain tools + metadata.

        No connection is made here — the converted tools open a fresh session
        per invocation from ``connection`` (stateless pattern). Tool names are
        prefixed with the server name (e.g. ``serena_read_file``) so MCP tools
        never collide with the agent's built-in tools.
        """
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

        # stdio servers get ONE session reused across calls. They are the ones
        # that hold state (a browser, an indexed project) and the ones where a
        # per-call session also means respawning the whole server process.
        # Remote transports reconnect cheaply and are left on the original
        # stateless path, so this change cannot regress them.
        session: Any = None
        if str(connection.get("transport", "stdio")) == "stdio":
            session = _SessionProxy(self._session_pool, server_name, connection)

        server_tools = [
            # Make tool failures non-fatal: an MCP/anyio error (e.g. a
            # playwright ERR_CONNECTION_REFUSED) can surface as a
            # BaseExceptionGroup that escapes LangGraph's ToolNode error
            # handling and aborts the whole turn. Wrapping each tool so it
            # returns the error as a string lets the model recover instead.
            _wrap_tool_error_handling(
                convert_mcp_tool_to_langchain_tool(
                    session,
                    raw,
                    connection=connection,  # type: ignore[arg-type]
                    server_name=server_name,
                    tool_name_prefix=True,
                )
            )
            for raw in raw_tools
        ]

        # Build metadata cache with correct server attribution
        for tool in server_tools:
            input_schema = {}
            if hasattr(tool, "args_schema") and tool.args_schema:
                try:
                    schema: Any = tool.args_schema
                    if hasattr(schema, "model_json_schema"):
                        schema = schema.model_json_schema()
                    if isinstance(schema, dict):
                        input_schema = schema.get("properties", {})
                except Exception:
                    pass

            self._tools_cache.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "server": server_name,
                    "input_schema": input_schema,
                }
            )

        return server_tools

    async def _discover_tools_async(self) -> None:
        """Async implementation of tool discovery.

        Servers whose tool schemas are in the disk cache (and whose connection
        config hasn't changed) are rebuilt WITHOUT connecting — this removes
        several seconds of stdio-server cold starts from every boot. Only
        cache misses do live discovery, parallelised with a concurrency cap.
        Cached servers are re-discovered in a background thread afterwards so
        the cache is at most one boot stale.
        """
        from novacode_cli.mcp.client import build_mcp_config_dict

        servers = self.mcp_config.list_servers()

        if not servers:
            return

        # Build combined config for all servers
        config_dict = build_mcp_config_dict(self.mcp_config)

        if not config_dict:
            return

        # Create the combined client
        self._client = MultiServerMCPClient(config_dict)

        # Reset the metadata cache so a re-run cannot accumulate duplicates.
        self._tools_cache = []

        # Split servers into schema-cache hits (rebuilt instantly, no network)
        # and misses (live discovery).
        schema_cache = _load_schema_cache()
        cached_raw: dict[str, list[MCPTool]] = {}
        to_discover: dict[str, dict[str, Any]] = {}
        for name, conn in config_dict.items():
            entry = schema_cache.get(name)
            if entry and entry.get("fingerprint") == _connection_fingerprint(conn):
                try:
                    cached_raw[name] = [MCPTool.model_validate(t) for t in entry["tools"]]
                    continue
                except Exception:  # noqa: BLE001 - malformed entry → live discovery
                    pass
            to_discover[name] = conn

        all_tools: list[BaseTool] = []
        for name, raw in cached_raw.items():
            all_tools.extend(self._convert_raw_tools(name, config_dict[name], raw))

        if to_discover:
            # Cap concurrent discovery to avoid overwhelming the host
            _discovery_semaphore = asyncio.Semaphore(3)

            async def _discover_one(
                server_name: str, connection: dict[str, Any]
            ) -> tuple[str, list[MCPTool] | None]:
                """Fetch raw tool schemas for a single MCP server."""
                async with _discovery_semaphore:
                    try:
                        return server_name, await _fetch_raw_tools(connection)
                    except Exception as e:
                        # Log the error but continue with other servers
                        error_msg = str(e)
                        if "TaskGroup" in error_msg or "unhandled errors" in error_msg:
                            error_msg = "Connection timeout or initialization error"
                        console.print(
                            f"[yellow]Warning: Failed to connect to "
                            f"MCP server '{server_name}': {error_msg}[/yellow]"
                        )
                        return server_name, None

            results = await asyncio.gather(
                *(_discover_one(name, conn) for name, conn in to_discover.items()),
                return_exceptions=True,
            )

            for r in results:
                if isinstance(r, BaseException):
                    continue
                server_name, raw = r
                if raw is None:
                    continue
                all_tools.extend(
                    self._convert_raw_tools(server_name, to_discover[server_name], raw)
                )
                schema_cache[server_name] = {
                    "fingerprint": _connection_fingerprint(to_discover[server_name]),
                    "tools": [t.model_dump(mode="json") for t in raw],
                    "ts": time.time(),
                }
            _save_schema_cache(schema_cache)

        # Store all tools for the agent
        self.tools = all_tools  # type: ignore

        # Mark discovery as complete to avoid re-discovering on every message
        self._tools_discovered = True

        # Refresh stale cached schemas in the background (disk cache only —
        # the tools registered above stay as built for this session). TTL-
        # gated so most boots spawn nothing.
        stale = {
            name: config_dict[name]
            for name in cached_raw
            if time.time() - schema_cache.get(name, {}).get("ts", 0)
            > _SCHEMA_REFRESH_TTL_SECONDS
        }
        if stale:
            # Stamp ts NOW (optimistically), before the refresh runs. The
            # refresh is a daemon thread — a short-lived process exits before
            # it finishes, the stamp never lands, and every subsequent process
            # re-triggers the refresh (re-spawning stdio servers, incl. slow
            # uvx git builds) in a stampede. Worst case of stamping first:
            # a failed refresh leaves schemas stale for one more TTL. Fine.
            for name in stale:
                if name in schema_cache:
                    schema_cache[name]["ts"] = time.time()
            _save_schema_cache(schema_cache)
            threading.Thread(
                target=self._refresh_schema_cache,
                args=(stale,),
                daemon=True,
                name="mcp-schema-refresh",
            ).start()

    def _refresh_schema_cache(self, servers: dict[str, dict[str, Any]]) -> None:
        """Re-discover the given servers and rewrite their cache entries.

        Runs in a daemon thread with its own event loop. Failures are silent
        (debug log) — the existing cache entry simply stays until a refresh
        succeeds or the connection config changes.
        """

        async def _refresh() -> dict[str, Any]:
            sem = asyncio.Semaphore(3)

            async def _one(name: str, conn: dict[str, Any]) -> tuple[str, Any]:
                async with sem:
                    try:
                        raw = await _fetch_raw_tools(conn)
                    except Exception:  # noqa: BLE001
                        logger.debug("MCP schema refresh failed for %s", name, exc_info=True)
                        return name, None
                    return name, {
                        "fingerprint": _connection_fingerprint(conn),
                        "tools": [t.model_dump(mode="json") for t in raw],
                        "ts": time.time(),
                    }

            results = await asyncio.gather(*(_one(n, c) for n, c in servers.items()))
            return {name: entry for name, entry in results if entry is not None}

        try:
            fresh_entries = asyncio.run(_refresh())
            if fresh_entries:
                # Single read-modify-write at the end to avoid clobbering
                # entries written by the boot path.
                cache = _load_schema_cache()
                cache.update(fresh_entries)
                _save_schema_cache(cache)
        except Exception:  # noqa: BLE001 - background refresh must never crash anything
            logger.debug("MCP schema refresh crashed", exc_info=True)

    async def abefore_agent(
        self,
        state: MCPState,
        runtime: Runtime,
    ) -> MCPStateUpdate | None:
        """Store MCP tools metadata in state before the agent run starts.

        ``abefore_agent`` is a valid ``AgentMiddleware`` hook — the previous
        ``on_session_start`` name was NOT a recognised hook, so it never fired,
        which left ``mcp_tools`` state empty and the MCP doc section missing.

        Tools are normally discovered eagerly at agent-build time so they are
        registered with the graph; this hook defensively ensures discovery has
        run and exposes the metadata via state for the system-prompt section.

        Args:
            state: Current agent state.
            runtime: The LangGraph runtime instance.

        Returns:
            State update with MCP tools metadata, or None if no tools.
        """
        await self._ensure_tools_discovered()

        if not self._tools_cache:
            return None

        return {"mcp_tools": self._tools_cache}

    def _format_servers_list(
        self,
        servers: dict[str, Any],
        tools_metadata: list[dict[str, Any]],
    ) -> str:
        """Format MCP servers and their tools for display in system prompt.

        Uses O(n) algorithm by pre-grouping tools by server name.

        Args:
            servers: Dictionary of server configurations
            tools_metadata: List of tool metadata

        Returns:
            Formatted string for system prompt
        """
        lines = []

        # O(n) - Group tools by server name using dict
        tools_by_server: dict[str, list[dict[str, Any]]] = {}
        for tool in tools_metadata:
            server_name = tool.get("server", "")
            if server_name not in tools_by_server:
                tools_by_server[server_name] = []
            tools_by_server[server_name].append(tool)

        for name, config in servers.items():
            lines.append(f"\n**{name}** ({config.transport})")

            if config.description:
                lines.append(f"  {config.description}")

            # O(1) - Get tools for this server from pre-grouped dict
            server_tools = tools_by_server.get(name, [])

            if server_tools:
                # Names only. Each bound tool already carries its own description
                # and parameter schema, so repeating them here cost ~25k chars a
                # turn for zero extra signal. What the schema does NOT say, and
                # what this list is for, is which server a tool came from.
                names = ", ".join(t["name"] for t in server_tools)
                lines.append(f"  Tools ({len(server_tools)}): {names}")
                # ponytail: names also appear under "More tools" when deferred; ~1.5k dup chars
            else:
                lines.append("  (No tools available)")

            lines.append("")

        return "\n".join(lines)

    def _inject_mcp_section(
        self, request: ModelRequest
    ) -> ModelRequest:
        """Inject MCP documentation into the system prompt (shared by sync/async paths).

        Uses a TTL-backed cache (``_mcp_section_cache``) to avoid re-rendering the
        Jinja template on every model turn. The cache has a sliding window — each
        hit extends its lifetime — so active sessions keep the section hot without
        re-rendering, while idle sessions let the cache expire naturally.

        Args:
            request: The model request to modify.

        Returns:
            The request with MCP section injected into ``system_prompt``, or the
            original request unchanged if no MCP tools are configured.
        """
        # Prefer tools recorded in agent state, but fall back to the discovery
        # cache. State is only populated by the (now-removed) session-start hook;
        # the cache is filled by eager discovery at agent-build time, so this
        # fallback keeps the MCP doc section working regardless.
        mcp_tools = request.state.get("mcp_tools") or self._tools_cache
        if not mcp_tools:
            return request

        import time
        current_time = time.time()

        # Sliding-window cache: refresh on access to keep hot during active use
        if (
            self._mcp_section_cache is not None
            and current_time - self._mcp_section_cache_time < self._mcp_section_cache_ttl
        ):
            self._mcp_section_cache_time = current_time
            mcp_section = self._mcp_section_cache
        else:
            servers = self.mcp_config.list_servers()
            servers_list = self._format_servers_list(servers, mcp_tools)
            # Only teach the browser workflow when those tools are actually
            # loaded — a section describing tools the model does not have is
            # worse than none (it invents calls that fail). Keyed on the tool
            # NAMES, not the server name, so it works whatever the user called
            # the server in mcp.json.
            has_playwright = any(
                str(t.get("name", "")).endswith("browser_navigate") for t in mcp_tools
            )
            # Same keying for the cua-driver computer-use guidance: the prefix is
            # whatever the user named the server ("cua-driver_" by default).
            cua_prefix = next(
                (
                    str(t["name"])[: -len("get_window_state")]
                    for t in mcp_tools
                    if str(t.get("name", "")).endswith("get_window_state")
                ),
                "",
            )
            mcp_section = render_template(
                "mcp.jinja",
                servers_list=servers_list,
                has_playwright=has_playwright,
                cua_prefix=cua_prefix,
            )

            self._mcp_section_cache = mcp_section
            self._mcp_section_cache_time = current_time

        # Track context usage (best-effort)
        try:
            from novacode_cli.context import ContextManager

            budget = ContextManager().budget()
            tokens_added = budget.track_middleware("MCPMiddleware", mcp_section)
            logger.debug(
                f"MCPMiddleware added {tokens_added} tokens to context "
                f"(total: {budget.total_tokens}/{budget.max_tokens})"
            )
        except ImportError:
            pass

        system_prompt = (
            request.system_prompt + "\n\n" + mcp_section
            if request.system_prompt
            else mcp_section
        )
        return request.override(system_prompt=system_prompt)  # type: ignore

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject MCP tool information into the model request system prompt.

        MCP tools are registered statically via ``self.tools`` at init time, so
        they are already present in ``request.tools``. This method only injects
        the MCP documentation section into the system prompt.

        Delegates to the shared ``_inject_mcp_section()`` so that the sync and
        async paths stay in lockstep (same caching, same injection logic).
        """
        return handler(self._inject_mcp_section(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """(async) Inject MCP tool information into the model request system prompt.

        Delegates to the shared ``_inject_mcp_section()`` so that the sync and
        async paths stay in lockstep (same caching, same injection logic).
        """
        return await handler(self._inject_mcp_section(request))


__all__ = ["MCPMiddleware"]
