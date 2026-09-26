"""Repair malformed tool-call history before every model call.

Two failure shapes kill a strict provider, and only some providers tolerate
them, so they surface only after a model/provider switch hands one provider's
history to another:

1. **An unanswered call** — an assistant message's ``tool_calls`` are not each
   followed by a matching tool message:

       400 invalid_request_error — An assistant message with 'tool_calls' must
       be followed by tool messages responding to each 'tool_call_id'.

   deepagents already ships that repair — ``PatchToolCallsMiddleware`` gives each
   unanswered call a synthetic "cancelled" result — but runs it in
   ``before_agent``, i.e. once per invocation, so a call left unanswered *during*
   a turn reaches the next model call unrepaired.

2. **An orphaned result** — a ``tool`` message with no preceding assistant
   ``tool_calls``. Reformatting history across providers can drop the typed
   call while the result survives:

       400 invalid_request_error — Messages with role 'tool' must be a response
       to a preceding message with 'tool_calls'.

   ``PatchToolCallsMiddleware`` only *adds* missing results; it never removes an
   orphan, so this shape reached the provider as-is. Prompt-cache compaction, a
   session-continuation window trim, or a provider whose converter stores calls
   in a non-standard place can all drop the call and leave the result behind.

This runs the upstream repair plus :func:`sanitize_history` in ``before_model``,
so it holds at the point that actually matters: what is about to be sent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentState
    from langgraph.runtime import Runtime


#: Thinking-model fields that live in ``additional_kwargs`` and are echoed back
#: on later requests (see ``utils/backend_patches``). They belong to the model
#: that produced them; a different provider must not be sent them.
_REASONING_FIELDS = ("reasoning_content", "reasoning")

#: Provider-specific content-block types (Anthropic extended thinking). They are
#: signed for the model that produced them and are not portable across providers.
_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")


def _call_ids(message: AnyMessage) -> list[str]:
    """The tool-call ids carried by *message*, if it is an assistant message.

    Includes ``invalid_tool_calls`` (malformed arguments): the upstream patcher
    synthesizes results for those too, so their result must not look orphaned.
    """
    if not isinstance(message, AIMessage):
        return []
    ids: list[str] = []
    for call in (
        *(getattr(message, "tool_calls", None) or ()),
        *(getattr(message, "invalid_tool_calls", None) or ()),
    ):
        cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
        if cid:
            ids.append(cid)
    return ids


def _promote_raw_tool_calls(message: AnyMessage) -> AnyMessage:
    """Move OpenAI-style raw ``tool_calls`` into typed ``AIMessage.tool_calls``.

    Some providers populate only ``additional_kwargs["tool_calls"]`` (the raw
    wire form) and leave the typed field empty. The next provider's converter
    reads the typed field, so it emits an assistant turn with no calls while the
    tool results that answered them remain — producing the orphaned result above.
    Promoting the raw calls restores the pairing.
    """
    if not isinstance(message, AIMessage) or getattr(message, "tool_calls", None):
        return message
    raw = (getattr(message, "additional_kwargs", None) or {}).get("tool_calls")
    if not raw:
        return message
    promoted: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                arguments = {"_raw": arguments}
        elif arguments is None and isinstance(function.get("args"), dict):
            arguments = function["args"]
        promoted.append(
            {
                "name": function.get("name") or entry.get("name") or "",
                "args": arguments if isinstance(arguments, dict) else {},
                "id": entry.get("id") or "",
                "type": "tool_call",
            }
        )
    if not promoted:
        return message
    return message.model_copy(update={"tool_calls": promoted})


def _origin_model(message: AnyMessage) -> str | None:
    """The model that produced *message*, from its response metadata, if known."""
    metadata = getattr(message, "response_metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    return metadata.get("model_name") or metadata.get("model")


def _strip_foreign_reasoning(message: AnyMessage, current_model: str | None) -> AnyMessage:
    """Drop provider-specific reasoning produced by a *different* model.

    Covers both places it can live: ``reasoning_content``/``reasoning`` in
    ``additional_kwargs`` (OpenAI-compatible thinking models, re-attached by the
    backend patch) and ``thinking`` content blocks (Anthropic extended thinking,
    which are signed for the model that produced them). After a switch that is
    wrong: the new provider never asked for it and some reject unknown fields or
    block types. Only strips when provenance is known and differs, so a thinking
    model that stays in use keeps its own chain of thought.
    """
    if not current_model or not isinstance(message, AIMessage):
        return message
    origin = _origin_model(message)
    if origin is None or origin == current_model:
        return message

    updates: dict[str, Any] = {}
    extra = getattr(message, "additional_kwargs", None)
    if isinstance(extra, dict) and any(k in extra for k in _REASONING_FIELDS):
        updates["additional_kwargs"] = {
            k: v for k, v in extra.items() if k not in _REASONING_FIELDS
        }
    content = getattr(message, "content", None)
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES
        for block in content
    ):
        filtered = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES)
        ]
        updates["content"] = filtered or ""
    if not updates:
        return message
    return message.model_copy(update=updates)


def sanitize_history(
    messages: list[AnyMessage], current_model: str | None = None
) -> list[AnyMessage]:
    """Make *messages* a well-formed sequence for any provider.

    - Promotes raw tool calls so the assistant turn and its results stay paired.
    - Drops a ``tool`` message that no preceding assistant message issued.
    - Strips reasoning echoed from a different model (see
      :func:`_strip_foreign_reasoning`).

    Returns the original list object when nothing changed, so callers can use
    identity to decide whether a rewrite is needed.
    """
    promoted = [_promote_raw_tool_calls(m) for m in messages]

    issued: set[str] = set()
    kept: list[AnyMessage] = []
    for message in promoted:
        issued.update(_call_ids(message))
        if isinstance(message, ToolMessage):
            if getattr(message, "tool_call_id", None) in issued:
                kept.append(message)
            # else: orphaned result → drop, or a strict provider rejects the whole request
        else:
            kept.append(message)

    if current_model:
        kept = [_strip_foreign_reasoning(m, current_model) for m in kept]

    if len(kept) == len(messages) and all(a is b for a, b in zip(kept, messages)):
        return messages
    return kept


class RepairToolCallsEachStep(PatchToolCallsMiddleware):
    """``PatchToolCallsMiddleware`` + history sanitation before every model call.

    A subclass rather than a reimplementation: the pairing logic (including the
    handling of ``invalid_tool_calls``) stays exactly upstream's, and inherits
    any fix to it. The distinct class name also keeps it from colliding with the
    instance deepagents already installs.
    """

    def __init__(self, *, current_model: str | None = None) -> None:
        """``current_model``: the model about to be called; enables cross-provider
        reasoning stripping (see :func:`sanitize_history`)."""
        super().__init__()
        self._current_model = current_model

    def before_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        patched = self.before_agent(state, runtime)
        messages = patched["messages"][1:] if patched else list(state["messages"])
        filled = [_fill_empty_result(m) for m in messages]
        sanitized = sanitize_history(filled, self._current_model)
        if patched is None and len(sanitized) == len(messages) and all(
            a is b for a, b in zip(sanitized, messages)
        ):
            return None
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *sanitized]}

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
