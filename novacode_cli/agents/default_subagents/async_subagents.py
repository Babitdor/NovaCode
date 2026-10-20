# Async Subagents for NOVA CLI
# These run on remote LangGraph servers in the background

import logging
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

from deepagents.middleware.async_subagents import AsyncSubAgent

logger = logging.getLogger(__name__)

# ── Base URL resolution ────────────────────────────────────────────────────────

#: Where the async-subagent LangGraph server lives when nothing says otherwise.
#: Every async graph is hosted by one server; routing is by
#: ``graph_id``, so a single base URL and port cover all of them.
DEFAULT_ASYNC_AGENT_BASE_URL = "http://localhost"
ASYNC_AGENT_PORT = 2024

#: How long to wait for the server's port to accept a connection. This runs on
#: the agent-build path, so it has to be short enough not to be felt; a
#: container that is up answers a local TCP connect in single-digit ms.
_PROBE_TIMEOUT_S = 0.4

_availability: bool | None = None


def _resolve_async_agent_url(port: int) -> str:
    """Resolve the LangGraph server URL for an async subagent.

    Priority:
    1. ``ASYNC_AGENT_BASE_URL`` environment variable (shared base, e.g.
       ``http://localhost`` or ``http://doc-agent`` in Docker)
    2. ``LANGGRAPH_API_URL`` environment variable (shared LangGraph Platform URL)
    3. :data:`DEFAULT_ASYNC_AGENT_BASE_URL`

    Previously this returned ``None`` when neither variable was set, which made
    every spec fall back to an in-process ASGI transport that only exists inside
    a ``langgraph dev`` process — so with the container running but no env var
    exported, delegation failed. The port is appended as ``{base}:{port}``.
    """
    base = (
        os.environ.get("ASYNC_AGENT_BASE_URL")
        or os.environ.get("LANGGRAPH_API_URL")
        or DEFAULT_ASYNC_AGENT_BASE_URL
    ).rstrip("/")
    # A base that already names a port wins — writing the obvious
    # ASYNC_AGENT_BASE_URL=http://localhost:2024 otherwise produced
    # "http://localhost:2024:2024", which no client can reach.
    if _has_port(base):
        return base
    return f"{base}:{port}"


def _has_port(base: str) -> bool:
    """Whether *base* already carries an explicit ``:port``."""
    try:
        return urlsplit(base).port is not None
    except ValueError:
        return False


def _own_server_planned() -> bool:
    try:
        from novacode_cli.agents import server_launcher

        return server_launcher.planned()
    except Exception:  # noqa: BLE001
        return False


def async_agents_available(*, refresh: bool = False) -> bool:
    """Whether the async-subagent server is actually reachable.

    True at once when Nova has its own server planned (it starts on the first
    dispatch). Otherwise this probes for one that is already running. Offering
    the agents with no server turns every delegation into a failed round-trip,
    so the specs are withheld instead and the agent uses its ordinary in-process
    subagents.

    One TCP connect, cached for the process: this sits on the agent-build path.
    """
    global _availability
    if _own_server_planned():
        # Nova will start its own server on the first dispatch: nothing to
        # probe yet, and the tools must be offered for that to ever happen.
        return True
    if _availability is not None and not refresh:
        return _availability
    try:
        parts = urlsplit(_resolve_async_agent_url(ASYNC_AGENT_PORT))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            # A base URL the client cannot use. Reaching some other server on
            # the default port would be worse than reporting it unavailable:
            # the specs would carry the unusable URL.
            msg = f"unusable async agent base URL: {parts.geturl()!r}"
            raise ValueError(msg)
        host, port = parts.hostname, parts.port or ASYNC_AGENT_PORT
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            _availability = True
    except (OSError, ValueError):  # unreachable, or a malformed base URL
        host = port = "?"
        _availability = False
    logger.debug("async subagent server at %s:%s reachable=%s", host, port, _availability)
    return _availability


#: The directory an already-running async server works in. Only needed for a
#: server Nova did not launch itself.
ASYNC_AGENT_ROOT_VAR = "NOVA_ASYNC_AGENT_ROOT"


def async_agents_see_workspace() -> bool:
    """Whether the async server works on the same files as this session.

    A server Nova launches itself is told the session's workspace, so it always
    does. One that was already running (started by hand, or on another machine)
    works on whatever directory it was started for, and Nova cannot ask it
    which: in the wrong project its agents would scout the wrong source and
    report it as findings, which is worse than not running. Such a server is
    used only when ``NOVA_ASYNC_AGENT_ROOT`` declares its directory and the
    workspace is that directory (or inside it).
    """
    if _own_server_planned():
        return True  # launched with NOVA_WORKSPACE_ROOT set to this workspace
    try:
        from novacode_cli.config.config import settings

        declared = os.environ.get(ASYNC_AGENT_ROOT_VAR, "").strip()
        if not declared:
            return False
        server_root = Path(declared).resolve()
        workspace = Path(settings.get_workspace_root()).resolve()
    except Exception:  # noqa: BLE001 — unknown roots: do not risk the wrong tree
        logger.debug("could not compare the async server root to the workspace", exc_info=True)
        return False
    return workspace == server_root or server_root in workspace.parents


def async_agents_usable() -> bool:
    """Reachable *and* looking at this workspace: the condition for offering them."""
    return async_agents_available() and async_agents_see_workspace()


def _resolve_async_agent_headers() -> dict[str, str]:
    """Resolve auth headers for the async subagent server.

    Uses ``LANGGRAPH_API_KEY`` if set, otherwise returns empty headers
    (assumes local/unauthenticated server).
    """
    api_key = os.environ.get("LANGGRAPH_API_KEY")
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


# ── Agent descriptions ─────────────────────────────────────────────────────────

RESEARCH_AGENT_DESCRIPTION = """An async agent that researches a question on the web in the background.

Use this agent when:
- A question needs several searches and sources read, which takes minutes
- You want sourced findings without stalling the current work
- The research is independent of the code in front of you

It searches, reads primary sources and reports findings with their sources. This is the
background form of the `web-researcher` subagent; `/research` still runs the full swarm.
"""

# ── Agent builders ─────────────────────────────────────────────────────────────


def _build_agent_spec(
    name: str,
    graph_id: str,
    description: str,
    port: int,
) -> AsyncSubAgent:
    """Build an AsyncSubAgent spec with resolved URL and headers."""
    return {
        "name": name,
        "description": description,
        "graph_id": graph_id,
        "url": _resolve_async_agent_url(port),
        "headers": _resolve_async_agent_headers(),
    }


def build_research_agent() -> AsyncSubAgent:
    """Build the web research async subagent config."""
    return _build_agent_spec(
        name="research-agent",
        graph_id="research-agent",
        description=RESEARCH_AGENT_DESCRIPTION,
        port=2024,
    )


def build_general_purpose_async_agent() -> AsyncSubAgent:
    """The default background delegate when no narrower specialist fits."""
    return _build_agent_spec(
        name="general-purpose-async",
        graph_id="general-purpose-async",
        description=(
            "General-purpose background agent for scoped coding, investigation, testing, "
            "research and analysis. Prefer start_async_task with this agent over synchronous "
            "general-purpose delegation; returns a task ID immediately so the main "
            "conversation stays available. Uses the workspace and unattended approval policy."
        ),
        port=2024,
    )


def _user_agent_spec(name: str, description: str) -> AsyncSubAgent:
    """Build the spec for a user-created async subagent.

    The graph id is the agent's own name: what the user typed is what they then
    see in ``/agent-server status`` and in the main agent's tool description.

    Private, and deliberately not named ``build_*_agent``: the shipped builders
    are zero-argument (they describe a graph in the package's own config, which
    a test pairs against that file by name), while this one describes a graph
    that only exists in a generated config.
    """
    return _build_agent_spec(
        name=name,
        graph_id=name.lower(),
        description=description,
        port=2024,
    )


def _user_agent_specs() -> list[AsyncSubAgent]:
    """The specs for every user ``agent.md`` marked ``async: true``.

    Empty when the user has none, which is the usual case. A user agent is an
    ordinary in-process subagent too: marking it async adds the background form
    rather than replacing the synchronous one, because long work and work that
    must finish inside this turn are both real needs.
    """
    from novacode_cli.agents.user_async_agents import (
        async_agent_description,
        collect_user_async_agents,
    )

    specs: list[AsyncSubAgent] = []
    # collect_user_async_agents already returns (graph_id, agent_md) pairs, and
    # only ones the server can actually serve, so there is no name to re-derive
    # and no graph_id to advertise that would 404 on dispatch.
    for graph_id, agent_md in collect_user_async_agents():
        try:
            description = async_agent_description(agent_md)
        except Exception:  # noqa: BLE001 — one unreadable file drops one agent
            logger.warning("could not describe async agent %r", graph_id, exc_info=True)
            continue
        specs.append(_user_agent_spec(graph_id, description))
    return specs


def retrieve_async_subagents() -> list[AsyncSubAgent]:
    """Return the async subagents, or ``[]`` when their server is not running.

    Async subagents run on remote Agent Protocol servers and execute in the
    background, returning a task ID immediately. With no server to run them
    (none planned, none answering) the tools would still be advertised and every
    delegation would fail, so nothing is returned and the agent falls back to
    its synchronous in-process subagents.

    Returns:
        List of AsyncSubAgent configurations, empty when the server is down.
    """
    if not async_agents_available():
        logger.info(
            "No async subagent server — using synchronous subagents. Install the "
            "agents-server extra (uv sync --extra agents-server) to enable "
            "background delegation."
        )
        return []
    if not async_agents_see_workspace():
        logger.info(
            "Async subagent server is rooted at a different project — using "
            "synchronous subagents, which work in this workspace. Set %s if the "
            "server does see this project.",
            ASYNC_AGENT_ROOT_VAR,
        )
        return []
    # The rest of the specialists are agent files in ~/.nova/agents/ marked
    # `async: only` (shipped in default_subagents/builtin/), served like any
    # user async agent.
    return [
        build_general_purpose_async_agent(),
        build_research_agent(),
        *_user_agent_specs(),
    ]
