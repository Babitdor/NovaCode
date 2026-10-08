"""Build a background graph from a prompt and a list of tool names.

Shared by the async agents that are just that: a prompt, Nova tools named by
string, and file access to the session's workspace. An agent that already exists
in-process (``default_subagents/prompt.py``) can be handed over as it is, so the
two forms never drift apart.

A declared name is resolved against two sources, because the in-process subagent
path does the same: ``novacode_cli.tools`` for Nova's own tools, and the user's
MCP config for ``serena_*`` / ``playwright_*`` / ``cua-driver_*`` (see
``_mcp_tools``). Only the first was consulted here, so an agent whose prompt is
written around symbol navigation ran in the background with no symbol tool.

Only work that is long and self-contained belongs here; an agent that edits code
interactively needs the approvals that do not reach this server.
"""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from novacode_cli.agents.async_context import AsyncModelOverrideMiddleware, NovaAsyncContext
from novacode_cli.agents.async_workspace import workspace_root
from novacode_cli.config.model_create import build_async_agent_model

BACKGROUND_NOTE = """

## You are running in the background

Nobody is watching this run and nobody can answer a question, so never ask one:
make the reasonable assumption, state it, and carry on. Your final message is the
whole deliverable. Whoever dispatched you sees only that, so make it a complete,
self-contained report rather than a note that work was done.
"""


def build_specialist_graph(
    name: str,
    definition: dict[str, Any],
    *,
    extra_tools: list[Any] | None = None,
    note: str = "",
) -> Any:
    """A background graph for the agent described by *definition*.

    Args:
        name: The graph's agent name (its id in ``langgraph.json``).
        definition: A ``{"prompt", "tools"}`` entry.
        extra_tools: Tools this background form needs beyond the named ones.
        note: Anything the background form does differently from the prompt.
    """
    import novacode_cli.tools as nova_tools
    from novacode_cli.agents.async_agents._mcp_tools import mcp_tools_for
    from novacode_cli.security.delegated_approval import DelegatedApprovalMiddleware
    from novacode_cli.skills.libraries import background_library

    declared = definition.get("tools", [])
    # File tools (ls / read_file / glob / grep / write_file) come from the
    # backend, so only the names that are real Nova tools are looked up.
    named = [getattr(nova_tools, n) for n in declared if hasattr(nova_tools, n)]
    # MCP tools are not in novacode_cli.tools — they are discovered from the
    # user's MCP config — so they are resolved separately. Best-effort: an
    # unreachable MCP server yields fewer tools, never a graph that fails to
    # build (which would stop the whole server from starting).
    mcp = mcp_tools_for(declared)
    return create_deep_agent(
        name=name,
        model=build_async_agent_model(),
        tools=[*named, *mcp, *(extra_tools or [])],
        system_prompt=definition["prompt"] + BACKGROUND_NOTE + note,
        backend=FilesystemBackend(root_dir=str(workspace_root()), virtual_mode=True),
        middleware=[AsyncModelOverrideMiddleware(), DelegatedApprovalMiddleware(), background_library(name, definition)],
        context_schema=NovaAsyncContext,
    )
