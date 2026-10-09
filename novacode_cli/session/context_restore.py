"""Seed restored history without merging it into a previous checkpoint."""

from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage


async def seed_restored_context(
    agent: Any, messages: list[BaseMessage], *, as_node: str | None = None,
) -> str:
    """Return a new thread ID only after its continuation state is written.

    Reusing a saved thread merges messages through LangGraph's reducer and also
    retains the old summarization cutoff. A rebuilt continuation needs neither.
    The durable session ID remains unchanged.
    """
    thread_id = str(uuid4())
    kwargs = {"as_node": as_node} if as_node is not None else {}
    await agent.aupdate_state(
        {"configurable": {"thread_id": thread_id}},
        values={"messages": messages},
        **kwargs,
    )
    return thread_id
