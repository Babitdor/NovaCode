"""Context-bound session tools; callers cannot choose a sender identity."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from typing import Any

from langchain.tools import tool

Endpoint = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
endpoint: ContextVar[Endpoint | None] = ContextVar("session_endpoint", default=None)


async def request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send through the current turn's identity-bound endpoint."""
    gateway = endpoint.get()
    if gateway is None:
        return {"error": "Cross-session communication requires connected Nova tabs."}
    return await gateway(method, payload)


@tool
async def list_sessions() -> dict[str, Any]:
    """List live project tabs with stable session IDs, projects and running status."""
    return await request("list", {})


@tool
async def send_session_message(to_session: str, message: str) -> dict[str, Any]:
    """Ask another project tab to work on an explicit message. Busy tabs queue it.

    Only this message is shared. Use read_session_messages to get linked results;
    do not wait in a polling loop. The recipient keeps its own tool approvals.
    """
    return await request("send", {"target": to_session, "text": message})


@tool
async def read_session_messages() -> dict[str, Any]:
    """Read unread requests and linked results belonging to this project session."""
    return await request("read", {})


@tool
async def reply_session_message(request_id: str, message: str) -> dict[str, Any]:
    """Reply to a request addressed to your own session, using its request ID."""
    return await request("reply", {"id": request_id, "text": message})


SESSION_TOOLS = (list_sessions, send_session_message, read_session_messages, reply_session_message)


async def session_events(
    events: AsyncGenerator[Any, None], route: Endpoint
) -> AsyncGenerator[Any, None]:
    """Bind only this turn; other running graph tasks keep their own identities."""
    token = endpoint.set(route)
    try:
        async with aclosing(events):
            async for event in events:
                yield event
    finally:
        endpoint.reset(token)
