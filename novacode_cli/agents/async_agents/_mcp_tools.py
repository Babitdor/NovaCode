"""MCP tools for a background (async) agent graph.

A background graph is built by :func:`build_specialist_graph`, which resolves the
agent's declared tool names against ``novacode_cli.tools``. MCP tools are not in
that module — they are discovered at runtime from ``~/.nova/mcp.json`` — so every
``serena_*`` / ``playwright_*`` / ``cua-driver_*`` name an agent declares used to
be dropped without a word. An agent whose prompt is written around symbol
navigation then ran in the background with no symbol tool at all.

The server process can fix this itself: it is a subprocess of the same
interpreter with the same environment (``server_launcher.build_env``), and the
MCP config is a plain file, so there is no IPC and nothing to pass between
processes. This module is the one place that turns "the names in an agent's
frontmatter" into real MCP tools, reusing the same discovery the in-process path
uses (``MCPMiddleware``) and the same server-prefix convention the in-process
grant uses (``core_agent._grant_mcp_tools``).

**Failure is never fatal here.** ``langgraph`` loads every graph in the config at
startup and refuses to start if one fails to load, so an MCP server that is
missing, slow or broken must degrade to "this agent has fewer tools" rather than
"no background agent can run". Every path in :func:`mcp_tools_for` returns a
list, and the empty list is a normal answer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

#: Discovery results, keyed by the sorted tuple of requested tool names.
#:
#: The server imports every registered graph at startup, and each graph would
#: otherwise run its own discovery — spawning the same MCP servers once per
#: agent. Cold-starting a stdio server (``npx``/``uvx``) is seconds, so that
#: multiplies badly. Keyed by the request rather than by server so two agents
#: asking for different subsets each get exactly their own tools.
_CACHE: dict[tuple[str, ...], list[BaseTool]] = {}


def mcp_tools_for(names: list[str] | tuple[str, ...]) -> list[BaseTool]:
    """The MCP tools among *names*, discovered from the user's MCP config.

    Args:
        names: Tool names as written in an ``agent.md``'s ``tools:`` frontmatter,
            e.g. ``["serena_find_symbol", "code_search"]``. Names that are not MCP
            tools are ignored here — the caller resolves those against
            ``novacode_cli.tools``.

    Returns:
        The discovered MCP tools whose names appear in *names*, in discovery
        order. Empty when there is no MCP config, no server could be reached, or
        none of *names* is an MCP tool. Never raises: a background agent with
        fewer tools is strictly better than a server that will not boot.
    """
    wanted = tuple(sorted({n for n in names if n}))
    if not wanted:
        return []
    if wanted in _CACHE:
        return _CACHE[wanted]

    tools = _discover(wanted)
    _CACHE[wanted] = tools
    return tools


def _discover(wanted: tuple[str, ...]) -> list[BaseTool]:
    """Run MCP discovery and keep the tools named in *wanted*.

    Split out so :func:`mcp_tools_for` stays a cache lookup plus one call, and so
    the whole discovery can be wrapped in a single guard.
    """
    try:
        from novacode_cli.mcp import get_shared_mcp_middleware

        middleware = get_shared_mcp_middleware()
        # The sync entry point: this runs at graph import, where there is no
        # running loop to await on. It does its own work in a dedicated thread.
        middleware._discover_tools_sync()
        available = list(middleware.tools)
    except Exception:  # noqa: BLE001 — no MCP tools is a valid outcome
        logger.warning(
            "could not discover MCP tools for a background agent; it will run without them",
            exc_info=True,
        )
        return []

    wanted_set = set(wanted)
    kept = [t for t in available if _tool_name(t) in wanted_set]
    if not kept and wanted:
        # Worth a line: the agent asked for tools that exist in no configured
        # server, which is usually a typo or a server the user has not added.
        logger.info(
            "none of the %d requested MCP tool(s) were found in the MCP config: %s",
            len(wanted),
            ", ".join(wanted),
        )
    return kept


def _tool_name(tool: object) -> str:
    """A tool's name, or ``""`` when it has none.

    Mirrors ``core_agent._tool_name``: the name is what the prefix match and the
    caller's request are both expressed in, so a tool without one is simply not
    a match rather than an error.
    """
    return str(getattr(tool, "name", "") or "")


def reset_cache() -> None:
    """Drop the discovery cache.

    For tests, and for a caller that has just changed the MCP config and wants
    the next graph build to see it.
    """
    _CACHE.clear()
