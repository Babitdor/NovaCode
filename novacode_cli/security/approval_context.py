"""Owner intent for approval review, separate from delegated task instructions."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ToolCallRequest

current_user_request: ContextVar[str | None] = ContextVar("approval_user_request", default=None)


def request_user_text(request: ToolCallRequest, *, delegated: bool = False) -> str:
    """Use host-bound intent; a child's own task message is never owner consent."""
    bound = current_user_request.get()
    if bound is not None:
        return bound
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None)
    owner = (
        context.get("user_request", "")
        if isinstance(context, dict)
        else getattr(context, "user_request", "")
    )
    if isinstance(owner, str) and owner:
        return owner
    if delegated:
        return ""
    from novacode_cli.core.streaming import is_internal_context_text

    state = getattr(request, "state", {}) or {}
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", "") != "human":
            continue
        content = message.content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        if isinstance(content, str) and not is_internal_context_text(content):
            return content
    return ""
