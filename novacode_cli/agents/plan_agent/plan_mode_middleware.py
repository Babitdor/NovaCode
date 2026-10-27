"""Execution-time plan restrictions for dedicated and foreground agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from novacode_cli.config.plan_mode import active_plan_session, plan_tool_rejection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from langgraph.types import Command


class PlanModeMiddleware(AgentMiddleware):
    """Enforce planning restrictions before execution, independently of approval settings."""

    def __init__(self, workspace_root: Path | None = None, *, always_on: bool = True) -> None:
        """Always guard plan agents; foreground agents follow the live session flag."""
        self._workspace_root = workspace_root
        self._always_on = always_on

    def _rejection(self, request: ToolCallRequest) -> ToolMessage | None:
        session = active_plan_session.get()
        if not self._always_on and not getattr(session, "plan_mode_enabled", False):
            return None
        call = request.tool_call
        target = getattr(request, "tool", None)
        reason = plan_tool_rejection(
            call.get("name", ""),
            call.get("args") or {},
            metadata=getattr(target, "metadata", None),
            workspace_root=self._workspace_root,
        )
        if reason is None:
            return None
        return ToolMessage(
            content=reason,
            tool_call_id=call.get("id", ""),
            name=call.get("name", ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Reject disallowed synchronous calls before executing any tool."""
        rejection = self._rejection(request)
        return rejection if rejection is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Apply the same restrictions to asynchronous and routed tool calls."""
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)
