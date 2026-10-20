"""Compatibility wrappers retain one-shot semantics without global identities."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command


from langchain.agents.middleware.types import AgentMiddleware

from novacode_cli.teams.gateway import current_binding

# Scope and display identity are host supplied; tool arguments cannot spoof them.
mailbox_identity: ContextVar[tuple[str, str, str, str] | None] = ContextVar(
    "nova_scoped_mailbox", default=None
)


class LegacyMailboxMiddleware(AgentMiddleware):
    """Give a one-shot specialist its own identity inside its parent's root."""

    def __init__(self, name: str) -> None:
        """Bind a host-owned specialist label, never a tool-supplied sender."""
        self.agent_name = name

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Apply the specialist scope and restore the caller after execution."""
        binding = current_binding.get()
        if binding is None:
            return await handler(request)
        identity = binding.identity
        token = mailbox_identity.set(
            (identity.project, identity.root, identity.team or "", self.agent_name)
        )
        # The specialist must not inherit the lead's team-tool authority.
        binding_token = current_binding.set(None)
        try:
            return await handler(request)
        finally:
            current_binding.reset(binding_token)
            mailbox_identity.reset(token)
