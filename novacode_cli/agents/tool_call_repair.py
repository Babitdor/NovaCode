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
from langchain_core.messages import AnyMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

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
        patched = self.before_agent(state, runtime)
        messages = patched["messages"][1:] if patched else list(state["messages"])
        filled = [_fill_empty_result(m) for m in messages]
        if patched is None and all(a is b for a, b in zip(filled, messages, strict=True)):
            return None
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *filled]}

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.before_model(state, runtime)


# DeepSeek rejects an empty tool result outright:
#     Message {'role': 'tool', 'content': None, ...} has no content.
# Tools return one legitimately — Serena's read_file on an empty file returns a
# single empty text block — and once it is in history every later request of
# the session fails. The repair is persisted, so saved sessions heal too.
EMPTY_TOOL_RESULT = "(no output)"


def _fill_empty_result(msg: AnyMessage) -> AnyMessage:
    if not isinstance(msg, ToolMessage):
        return msg
    content = msg.content
    if isinstance(content, str):
        empty = not content.strip()
    else:
        empty = all(
            isinstance(block, dict)
            and block.get("type") == "text"
            and not str(block.get("text", "")).strip()
            for block in content
        )
    return msg.model_copy(update={"content": EMPTY_TOOL_RESULT}) if empty else msg
