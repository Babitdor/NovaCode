"""Run one of Nova's in-process specialists as a background graph.

The specialists (``default_subagents/prompt.py``) are defined once: a prompt and
a list of tool names. This turns such a definition into an async graph, so the
same agent can be dispatched with ``task`` (wait for the answer) or with
``start_async_task`` (carry on, be told when it finishes) without a second
prompt to keep in step.

Only specialists whose work is long and self-contained are exposed this way; one
that edits code interactively needs the approvals that do not reach this server.
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
    """A background graph for the specialist described by *definition*.

    Args:
        name: The graph's agent name (its id in ``langgraph.json``).
        definition: The specialist's ``{"prompt", "tools"}`` entry.
        extra_tools: Tools this background form needs beyond the named ones.
        note: Anything the background form does differently from the prompt.
    """
    import novacode_cli.tools as nova_tools

    # File tools (ls / read_file / glob / grep / write_file) come from the
    # backend, so only the names that are real Nova tools are looked up.
    named = [getattr(nova_tools, n) for n in definition.get("tools", []) if hasattr(nova_tools, n)]
    return create_deep_agent(
        name=name,
        model=build_async_agent_model(),
        tools=[*named, *(extra_tools or [])],
        system_prompt=definition["prompt"] + BACKGROUND_NOTE + note,
        backend=FilesystemBackend(root_dir=str(workspace_root()), virtual_mode=True),
        middleware=[AsyncModelOverrideMiddleware()],
        context_schema=NovaAsyncContext,
    )
