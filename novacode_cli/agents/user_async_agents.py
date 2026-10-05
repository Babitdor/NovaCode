"""User-created background (async) subagents, built from the same ``agent.md``.

An ``agent.md`` (``agents/agent_file.py``) holds a prompt and a tool list, which
is exactly what a background graph is built from: the shipped async agents are
all ``build_specialist_graph(name, {"prompt": ..., "tools": [...]})``
(``agents/async_agents/_specialist.py``). Setting ``async: true`` in the
frontmatter therefore says "also run this one on the agent server", with no
second file format and no second create flow.

The flag is the whole contract. An agent without it keeps the behaviour it
always had: an in-process subagent on the ``task`` tool, same turn, full tool
set. Nothing here is consulted unless the flag is set, so a session with no
async agents behaves exactly as it did before.

Serving one is the launcher's problem, not this module's: see
``agents/async_agents/_config.py`` for the generated graph modules and config,
and ``server_launcher.py`` for starting the server with them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The frontmatter key that makes an agent a background one.
ASYNC_KEY = "async"

#: How a user async agent is framed to the main agent, mirroring the wording the
#: shipped async agents use. The main agent reads only this string to decide
#: whether delegating is worth the extra round trip.
BACKGROUND_FRAMING = (
    "An async (background) agent. It runs on the local agent server, returns "
    "immediately, and reports when it finishes, so the session keeps working "
    "while it does. Delegate work that is long and self-contained."
)

#: Reserved by the launcher: the generated graph modules live here.
_MODULE_DIR = "user_async"

#: Characters allowed in a generated module's filename, derived from the agent
#: name. A name that cannot be expressed this way is refused by
#: :func:`graph_id_for` rather than escaped into something surprising.
_SAFE_NAME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


#: What a user's async agent is called on the server. Its name, verbatim, so the
#: graph id in ``/agent-server status`` and in the main agent's tool description
#: are the same string the user typed. Lowercase because that is the convention
#: every shipped graph id follows.
def graph_id_for(name: str) -> str:
    """The server-side graph id for the async agent *name*.

    Args:
        name: The agent's name, as it appears in ``agent.md``'s directory.

    Returns:
        The graph id to register it under.

    Raises:
        ValueError: The name cannot be a graph id or a module filename, or it
            collides with a shipped graph id. A collision is an error rather
            than a silent override: the generated config lists both, and one
            winning quietly would make a user's agent look absent.
    """
    if not name or not set(name) <= _SAFE_NAME:
        message = (
            f"{name!r} cannot be an async agent name: use letters, numbers, hyphens or underscores"
        )
        raise ValueError(message)

    graph_id = name.lower()
    shipped = shipped_graph_ids()
    if graph_id in shipped:
        message = (
            f"{name!r} collides with the built-in async agent {graph_id!r}; choose another name"
        )
        raise ValueError(message)
    return graph_id


def shipped_graph_ids() -> frozenset[str]:
    """The graph ids the package ships, which a user's agent cannot take."""
    return frozenset(_package_config_graphs())


def _package_config_path() -> Path:
    return Path(__file__).resolve().parent / "async_agents" / "langgraph.json"


def _package_config_graphs() -> dict[str, str]:
    """``{graph_id: "./module.py:graph"}`` from the shipped config, ``{}`` if unreadable.

    Read from the package's own file rather than hardcoded, so the two can never
    disagree: a shipped agent added there is protected by name automatically.
    """
    try:
        data = json.loads(_package_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config just protects nothing
        logger.debug("could not read the shipped async agent config", exc_info=True)
        return {}
    graphs = data.get("graphs") if isinstance(data, dict) else None
    return dict(graphs) if isinstance(graphs, dict) else {}


def is_async(front: dict[str, Any]) -> bool:
    """Whether *front* (an ``agent.md`` frontmatter) marks a background agent.

    Tolerant on purpose: this key is written by hand as often as by the create
    modal, and ``true``/``True``/``"yes"``/``1`` all mean the same thing to the
    person writing it. Anything unrecognised is False — the agent stays an
    ordinary in-process subagent, which is the safe reading of a typo.
    """
    value = front.get(ASYNC_KEY)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "on", "1"}
    return False


def collect_user_async_agents() -> list[tuple[str, Path]]:
    """Every user agent that can actually be served, keyed by graph id.

    Returns:
        ``(graph_id, agent_md_path)`` sorted by name, with no duplicates.

    An agent is excluded, with a log line, rather than raised on when:

    - its ``agent.md`` cannot be read, or does not set ``async: true``;
    - its name cannot be a graph id, or collides with a shipped agent;
    - its name collides with another *user* agent (two agents named ``Alpha``
      and ``alpha``, or one name present in both global and project scope, map
      to one graph id: one file would silently overwrite the other);
    - it has no system prompt, so there is no graph to build.

    That last case is why this is not simply "every agent with the flag". The
    server loads every registered graph at startup and refuses to start if one
    of them fails to load, so registering an agent that cannot build would take
    the other graphs down with it. Excluding it here is the only place where
    the decision is cheap and per-agent.
    """
    from novacode_cli.config.config import settings

    try:
        found = settings.get_all_agents()
    except Exception:  # noqa: BLE001 — no config means no user async agents
        logger.debug("could not enumerate user agents", exc_info=True)
        return []

    out: list[tuple[str, Path]] = []
    seen: dict[str, str] = {}
    for name, agent_dir, _scope in sorted(found, key=lambda a: a[0].lower()):
        agent_md = agent_dir / "agent.md"
        # read_agent_safe, not read_agent: an unreadable file must not raise here,
        # and an agent whose frontmatter does not parse at all simply is not an
        # async agent, which is the right reading of a file we cannot understand.
        front, body = read_agent_safe(agent_md)
        if not is_async(front):
            continue
        try:
            graph_id = graph_id_for(name)
        except ValueError as exc:
            logger.warning("skipping async agent: %s", exc)
            continue
        if graph_id in seen:
            logger.warning(
                "skipping async agent %r: %r is already served by %r",
                name,
                graph_id,
                seen[graph_id],
            )
            continue
        if not body.strip():
            logger.warning(
                "skipping async agent %r: it has no system prompt in %s, so there "
                "is nothing for a background agent to run",
                name,
                agent_md,
            )
            continue
        seen[graph_id] = name
        out.append((graph_id, agent_md))
    return out


def read_agent_safe(agent_md: Path) -> tuple[dict[str, Any], str]:
    """``(frontmatter, prompt)`` of an ``agent.md``, never raising.

    Reading one user file must not be able to fail a whole agent build, so this
    returns empty frontmatter for an unreadable file. Callers that need to know
    the file was missing ask for it another way (an absent ``agent.md`` is
    already filtered out by :func:`settings.get_all_agents`).
    """
    from novacode_cli.agents.agent_file import read_agent

    try:
        return read_agent(agent_md)
    except Exception:  # noqa: BLE001 — a hand-written file may be mid-edit
        logger.warning("could not read agent file %s", agent_md, exc_info=True)
        return {}, ""


def async_agent_description(agent_md: Path) -> str:
    """The description the main agent sees for a user async agent.

    The user's own description, with the background framing appended, so the
    main agent weighs a delegation correctly: it is not a subagent that answers
    inside this turn.
    """
    front, _body = read_agent_safe(agent_md)
    description = str(front.get("description") or "").strip()
    return f"{description}\n\n{BACKGROUND_FRAMING}" if description else BACKGROUND_FRAMING


def build_user_graph(name: str, agent_md: Path | str) -> Any:  # noqa: ANN401 — a compiled graph, whose type is deepagents' business
    """The background graph for the async agent *name*, read from its ``agent.md``.

    This is what a generated module in the launcher's temp directory calls at
    import time, which is why it takes the path rather than a parsed agent: the
    prompt is read inside the server process, from the file the user edits, and
    never inlined into generated source.

    Args:
        name: The graph's name, which is also its id in ``langgraph.json``.
        agent_md: Path to the agent's ``agent.md``.

    Returns:
        A compiled deep agent, built exactly like the shipped async agents.

    Raises:
        ValueError: The ``agent.md`` has no system prompt. An empty prompt would
            otherwise produce a graph that runs and reports nothing, which looks
            like the agent ignoring its task.
    """
    from novacode_cli.agents.async_agents._specialist import build_specialist_graph

    path = Path(agent_md)
    _front, body = read_agent_safe(path)
    prompt = body.strip()
    if not prompt:
        message = f"async agent {name!r} has no system prompt in {path}"
        raise ValueError(message)

    # An absent `tools:` means every tool, exactly as for an in-process agent
    # (agent_tools returns None). Passing None through would also be correct
    # here, but resolving it to [] below keeps the meaning explicit at the one
    # place that reads it. A declared name is resolved against novacode_cli.tools
    # and against the user's MCP config (see async_agents/_mcp_tools), so the
    # background form gets the same tools the in-process subagent does.
    chosen = _chosen_tools(path)
    return build_specialist_graph(
        name,
        {"prompt": prompt, "tools": chosen if chosen is not None else []},
    )


def _chosen_tools(agent_md: Path) -> list[str] | None:
    """The tool names in ``agent_md``'s frontmatter, or None for "every tool".

    Reads the raw text rather than the parsed frontmatter because ``agent_tools``
    is what the in-process agent path uses, so both kinds of agent agree on how
    a tool list is spelled. Guarded because the file was already read once by
    the caller and can vanish between the two reads if the user is editing it.
    """
    from novacode_cli.agents.agent_file import agent_tools

    try:
        return agent_tools(agent_md.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("could not re-read %s to resolve its tools", agent_md)
        return None


def module_name_for(name: str) -> str:
    """The generated module's filename stem for the async agent *name*.

    Keeps the graph name readable in a traceback while staying a valid Python
    The prefix is what makes the stem a valid Python identifier: a graph id may
    legally start with a digit, which the stem on its own would not. A hyphen is
    allowed to survive into the filename (``agent_my-notes.py``) because the
    server derives its own module name from the path and never imports this file
    by stem.
    """
    return f"agent_{name.lower()}"
