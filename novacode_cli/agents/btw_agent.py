"""Lightweight side-channel agent for /btw quick questions.

A minimal agent with only DuckDuckGo web search — no filesystem tools, no HITL,
no skills, no subagents. Runs on its own InMemorySaver so it is fully isolated
from the main conversation checkpointer and can run concurrently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents.graph import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.agents.model_retry import NovaModelRetryMiddleware as ModelRetryMiddleware
from novacode_cli.errors import is_retryable_model_error

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.pregel import Pregel

__all__ = ["create_btw_agent"]

_BTW_SYSTEM_PROMPT = """\
You are a quick-answer assistant embedded in a coding terminal (Nova).
The user is in the middle of a separate conversation and asked you a side question.

Guidelines:
- Answer concisely and directly — one short paragraph or a tight list.
- Use web search when you need current or factual information.
- Do NOT reference any prior coding context unless the user mentions it.
- No preamble ("Great question!", "Sure!") and no trailing offers to help further.
- If the question is simple enough to answer from knowledge, skip the search.
"""


def create_btw_agent(model: BaseChatModel) -> tuple[Pregel, Any]:
    """Create the /btw side-channel agent.

    Returns (agent, backend). The backend is None because the btw agent never
    touches the filesystem — it only does web search.
    """
    from novacode_cli.tools.web_tools import duckduckgo_search, web_search

    tools = [duckduckgo_search, web_search]

    agent = create_deep_agent(
        name="btw-agent",
        model=model,
        system_prompt=_BTW_SYSTEM_PROMPT,
        tools=tools,
        checkpointer=InMemorySaver(),
        backend=None,
        store=None,
        interrupt_on={},  # fully auto — no HITL pauses
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

    return agent, None
