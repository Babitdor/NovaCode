"""Repair unanswered tool calls before every model call, not just once per turn.

OpenAI-strict endpoints reject a history in which an assistant message's
``tool_calls`` are not each followed by a matching tool message:

    400 invalid_request_error — An assistant message with 'tool_calls' must be
    followed by tool messages responding to each 'tool_call_id'.

deepagents already ships the repair — ``PatchToolCallsMiddleware`` gives each
unanswered call a synthetic "cancelled" result — but runs it in
``before_agent``, i.e. once at the start of an invocation. A call left unanswered
*during* a turn (a cancelled tool, a tool that returns a ``Command`` carrying no
``ToolMessage``, one call of a parallel batch that never reports back) therefore
reaches the very next model call unrepaired, and a strict provider kills the
turn. Lenient ones (Ollama, some gateway upstreams) accept the malformed history
silently, which is why this surfaces only on some models.

This runs the identical, upstream-tested repair in ``before_model`` as well, so
it holds at the point that actually matters: what is about to be sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentState
    from langgraph.runtime import Runtime


class RepairToolCallsEachStep(PatchToolCallsMiddleware):
    """``PatchToolCallsMiddleware`` applied before every model call.

    A subclass rather than a reimplementation: the pairing logic (including the
    handling of ``invalid_tool_calls``) stays exactly upstream's, and inherits
    any fix to it. The distinct class name also keeps it from colliding with the
    instance deepagents already installs.
    """

    def before_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)
