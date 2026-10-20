"""The background dreamer: memory consolidation on its own agent and thread.

An automatic dream must not touch the user's conversation or project, and must
never stop to ask for approval while nobody is watching. So it gets its own
agent: file tools confined to the memory directory (served at ``/memories/``,
the paths the consolidation prompt uses) plus the ``memory_wiki`` tool, and no
shell, no project files, no subagents and no HITL. It runs on an InMemorySaver,
isolated from the main checkpointer, like the /btw agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.graph import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.agents.model_retry import NovaModelRetryMiddleware as ModelRetryMiddleware
from novacode_cli.errors import is_retryable_model_error

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel
    from langgraph.pregel import Pregel

__all__ = ["BACKGROUND_NOTE", "create_dream_agent"]

_SYSTEM_PROMPT = """\
You are performing a dream: a reflective pass over your memory files. Synthesize
what you have learned recently into durable, well-organized memories so that
future sessions can orient quickly. You run in the background while the user
works; nobody will answer questions. Work only under /memories/.
"""

#: Appended to the consolidation prompt for a background run.
BACKGROUND_NOTE = """\
## Running in the background

- Nobody is watching: do not ask questions. Record any conflict you cannot
  resolve in your report and leave both versions in place.
- `memory_search` and `search_tools` are not available here. Find overlapping
  notes with `grep` and `glob` under `/memories/`; `memory_wiki` is bound directly.
- Convert relative dates ("yesterday", "last week") to absolute dates.
- End with a report of at most five lines.
"""


def create_dream_agent(model: BaseChatModel, agent_dir: Path) -> Pregel:
    """Build the dream agent over ``agent_dir`` (the agent's memory root)."""
    from novacode_cli.tools.memory_wiki_tools import create_memory_wiki_tool

    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/memories/": FilesystemBackend(root_dir=str(agent_dir), virtual_mode=True)},
    )
    agent: Any = create_deep_agent(
        name="dream-agent",
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        tools=[create_memory_wiki_tool(agent_dir)],
        checkpointer=InMemorySaver(),
        backend=backend,
        store=None,
        interrupt_on={},  # background: never pause for approval
        subagents=[],
        middleware=[
            ModelRetryMiddleware(
                max_retries=3,
                retry_on=is_retryable_model_error,
                on_failure="error",
                backoff_factor=1.5,
                initial_delay=0.5,
            ),
        ],
    )
    return agent
