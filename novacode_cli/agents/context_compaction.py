"""Nova's context compaction: one middleware for everything that shrinks a turn.

Before every model call the conversation goes through three stages, cheapest
first, each only as far as the context pressure requires:

1. **Prune old tool results.** Results older than the last few are moved out to
   ``/cleared/`` and replaced by a line naming the file
   (:mod:`novacode_cli.agents.tool_offload`). Which results go is age by
   default; with tool verdicts enabled (``/decisions``), a System One model
   scores each one and only those it calls stale are moved
   (:mod:`novacode_cli.agents.tool_verdicts`).
2. **Trim old tool-call arguments.** A 2 KB heredoc the agent wrote forty steps
   ago is still in every request. Outside the recent tail, long string
   arguments of *any* tool are cut to their first characters.
3. **Summarize.** Everything before the recent tail is replaced by one message:
   the user's own messages verbatim, a model-written summary of the rest, the
   files touched, and the path of the saved transcript.

This replaces LangChain's ``ContextEditingMiddleware`` and deepagents'
``SummarizationMiddleware``. What is different, and why:

* **The summary is Nova's.** Same engine as ``/compact``
  (:func:`novacode_cli.compaction.summarize_conversation`): Nova's prompt,
  chunked when the history is larger than the summarizer's own window, retried
  on transient errors. The library sent the whole evicted history in one call.
* **The user's words survive.** A paraphrased task loses constraints, and on a
  long unattended run the task is the first message, the first thing evicted.
* **A failed summary never fails the turn.** The turn goes on unsummarized and
  the summary is retried later; after repeated failures, or when the provider
  rejects the request as too long, a mechanical digest (the calls made and how
  each ended) is used instead of a model.
* **It does not thrash.** Nothing is summarized unless enough would be removed
  to be worth a model call.
* **Sizes are calibrated.** Characters/4 undercounts code and JSON, and this
  middleware runs before others add to the system prompt, so the estimate is
  scaled by what the provider actually reported for the previous request.
* **The summary call does not stream.** It is tagged ``nostream``, so its text
  never appears in the transcript as if the assistant had said it.

The state contract is the library's (``_summarization_event`` with
``cutoff_index``, ``summary_message``, ``file_path``): messages are never
deleted from state, the view sent to the model is rebuilt from the event on
every call, and everything that already reads the event (``/context``, the
compaction notice, ``/compact``, resume) keeps working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command

from novacode_cli.compaction import (
    COMPACTION_SUMMARY_MARKER,
    KEEP_TAIL_FRACTION,
    KEEP_TAIL_MAX_TOKENS,
    _continuation_framing,
    _format_message_content,
    _historical_user_requests,
    _rehydration_note,
    _render_tool_call,
    _window_tokens,
    summarize_conversation,
)
from novacode_cli.core.streaming import is_internal_context_text

logger = logging.getLogger(__name__)

#: Marks the summary message so every surface can tell it from a user's message.
SUMMARY_SOURCE = "summarization"

#: A string argument of an old tool call longer than this is cut to its head.
MAX_ARG_CHARS = 2_000
ARG_HEAD_CHARS = 200

#: One user message is quoted up to this many characters in the summary.
MAX_USER_MESSAGE_CHARS = 4_000

#: Failed summary attempts before the mechanical digest is used instead.
FAILURES_BEFORE_DIGEST = 3

#: How many calls the mechanical digest lists.
DIGEST_LINES = 80

#: A tool result in the kept tail is clipped to this when the provider has
#: rejected the request as too long.
OVERFLOW_RESULT_HEAD = 3_000
OVERFLOW_RESULT_TAIL = 1_500


class CompactionState(AgentState):
    """Agent state plus what compaction remembers between model calls."""

    _summarization_event: Annotated[NotRequired[dict[str, Any] | None], PrivateStateAttr]
    """The latest summary: ``cutoff_index``, ``summary_message``, ``file_path``."""

    _compaction_backoff: Annotated[NotRequired[dict[str, int] | None], PrivateStateAttr]
    """After a failed summary: ``failures`` so far, and ``retry_at`` (message count)."""


@dataclass(frozen=True)
class Limits:
    """Token thresholds for one context window."""

    window: int
    #: Summarize at or above this.
    trigger: int
    #: Prune tool results and trim old arguments at or above this.
    prune: int
    #: Recent messages worth this much stay verbatim.
    keep: int


@dataclass
class _Plan:
    #: What to send if nothing is summarized (pruned and trimmed as needed).
    messages: list[AnyMessage]
    #: ``cutoff_index`` of the summary already in ``messages[0]``, if any.
    prior_cutoff: int | None
    #: Index in ``messages`` where the kept tail starts; 0 means do not summarize.
    cutoff: int
    limits: Limits
    #: Unscaled size estimate of ``messages`` plus system prompt and tools.
    raw_tokens: int


def limits_for(window: int, *, prune: int | None = None) -> Limits:
    """Thresholds for ``window``, the same ones ``/context`` shows."""
    from novacode_cli.context import CONTEXT_EDIT_TRIGGER_FRACTION, compact_threshold_pct

    trigger = int(window * compact_threshold_pct(window) / 100)
    if prune is None:
        # Below the summary trigger even on a small window, where the reserve
        # pulls that trigger down (see core_agent._context_edit_trigger).
        prune = max(
            int(window * 0.25),
            min(int(window * CONTEXT_EDIT_TRIGGER_FRACTION), trigger - 2_000),
        )
    keep = max(1, min(KEEP_TAIL_MAX_TOKENS, int(window * KEEP_TAIL_FRACTION)))
    return Limits(window=window, trigger=trigger, prune=prune, keep=keep)


def _text(message: AnyMessage) -> str:
    return _format_message_content(message.content)


def _is_summary(message: AnyMessage) -> bool:
    return (
        isinstance(message, HumanMessage)
        and message.additional_kwargs.get("lc_source") == SUMMARY_SOURCE
    )


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _abridge(text: str, limit: int) -> str:
    """Head and tail of ``text`` with the omitted middle counted."""
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.75), int(limit * 0.2)
    return f"{text[:head]}\n[… {len(text) - head - tail} characters omitted …]\n{text[-tail:]}"


def _encoded_image_chars(message: AnyMessage) -> int:
    """Count inline image bytes that LangChain's token estimate largely ignores."""
    if not isinstance(message.content, list):
        return 0
    total = 0
    for block in message.content:
        if not isinstance(block, Mapping) or block.get("type") not in {
            "image",
            "image_url",
            "input_image",
        }:
            continue
        payload = block.get("base64") or block.get("image_url") or block.get("url")
        if isinstance(payload, Mapping):
            payload = payload.get("url") or payload.get("data")
        source = block.get("source")
        if not payload and isinstance(source, Mapping):
            payload = source.get("data")
        if isinstance(payload, str):
            if payload.startswith("data:"):
                payload = payload.partition(",")[2]
            elif payload.startswith(("http://", "https://")):
                continue
            total += len(payload)
    return total


def _request_size_tokens(messages: Sequence[AnyMessage]) -> int:
    """Budget model tokens and encoded image transport size together."""
    image_chars = sum(_encoded_image_chars(message) for message in messages)
    return count_tokens_approximately(messages) + (image_chars + 3) // 4


def effective_view(
    messages: Sequence[AnyMessage], event: object
) -> tuple[list[AnyMessage], int | None]:
    """The conversation as the model sees it: the summary, then what followed it.

    Returns the messages and the event's cutoff (``None`` when no usable event).
    """
    if not isinstance(event, Mapping):
        return list(messages), None
    cutoff = event.get("cutoff_index")
    summary = event.get("summary_message")
    if not isinstance(cutoff, int) or cutoff < 0 or summary is None:
        return list(messages), None
    return [summary, *messages[cutoff:]], cutoff


def tail_start(messages: Sequence[AnyMessage], keep_tokens: int) -> int:
    """Index where the verbatim tail begins.

    The tail never starts on a tool result (its call would be summarized away
    and the provider would reject the orphan), always holds the latest exchange
    even when that alone is over budget, and never reaches index 0 — something
    has to be left to summarize.
    """
    start = len(messages)
    size = 0
    for index in range(len(messages) - 1, 0, -1):
        size += _request_size_tokens([messages[index]])
        if size > keep_tokens and start < len(messages):
            break
        if not isinstance(messages[index], ToolMessage):
            start = index
    return min(start, pending_tool_start(messages))


def pending_tool_start(messages: Sequence[AnyMessage]) -> int:
    """Unresolved tool calls and everything following them remain verbatim."""
    completed = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and any(
            call.get("id") not in completed for call in message.tool_calls or []
        ):
            return index
    return len(messages)


def trim_old_arguments(messages: list[AnyMessage], upto: int) -> list[AnyMessage]:
    """Cut long string arguments of tool calls made before ``messages[upto]``."""
    out = messages
    for index in range(min(upto, len(messages))):
        message = messages[index]
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        calls = [_trim_call(call) for call in message.tool_calls]
        if any(new is not old for new, old in zip(calls, message.tool_calls, strict=True)):
            if out is messages:
                out = list(messages)
            out[index] = message.model_copy(update={"tool_calls": calls})
    return out


def _trim_call(call: Any) -> Any:  # noqa: ANN401 - a ToolCall dict
    args = call.get("args") or {}
    trimmed = {}
    changed = False
    for key, value in args.items():
        if isinstance(value, str) and len(value) > MAX_ARG_CHARS:
            dropped = len(value) - ARG_HEAD_CHARS
            trimmed[key] = (
                f"{value[:ARG_HEAD_CHARS]}… [{dropped} more characters of this old "
                "call's argument trimmed to save context]"
            )
            changed = True
        else:
            trimmed[key] = value
    return {**call, "args": trimmed} if changed else call


def _clip_results(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Clip large tool results; used only once the provider said "too long"."""
    limit = OVERFLOW_RESULT_HEAD + OVERFLOW_RESULT_TAIL
    out = list(messages)
    for index, message in enumerate(out):
        if not isinstance(message, ToolMessage):
            continue
        if _encoded_image_chars(message):
            blocks = [
                {"type": "text", "text": "[image omitted: provider rejected an oversized request]"}
                if isinstance(block, Mapping)
                and block.get("type") in {"image", "image_url", "input_image"}
                else block
                for block in message.content
            ]
            out[index] = message.model_copy(update={"content": blocks})
            continue
        text = _text(message)
        if len(text) > limit + 200:
            clipped = (
                f"{text[:OVERFLOW_RESULT_HEAD]}\n[… {len(text) - limit} characters removed: "
                f"the request was too long for the model …]\n{text[-OVERFLOW_RESULT_TAIL:]}"
            )
            out[index] = message.model_copy(update={"content": clipped})
    return out


def user_messages(head: Sequence[AnyMessage], budget_chars: int) -> list[str]:
    """Carry all user constraints forward; the legacy budget never abridges them."""
    del budget_chars  # legacy compatibility parameter; constraints never truncate
    said: list[str] = []
    for message in head:
        if _is_summary(message):
            said.extend(message.additional_kwargs.get("user_messages") or [])
        elif isinstance(message, HumanMessage) and not message.additional_kwargs.get("lc_source"):
            text = _text(message)
            # Skill hints, memory and the like are injected as human messages.
            if text and not is_internal_context_text(text):
                said.append(text)
    # A size budget may trigger a refusal, never deletion of user constraints.
    return said


def user_content_blocks(head: Sequence[AnyMessage]) -> list:
    """Preserve original multimodal blocks alongside protected user quotations."""
    from copy import deepcopy

    blocks = []
    for message in head:
        if _is_summary(message):
            blocks.extend(message.additional_kwargs.get("user_content_blocks") or [])
        elif (
            isinstance(message, HumanMessage)
            and not message.additional_kwargs.get("lc_source")
            and isinstance(message.content, list)
        ):
            blocks.extend(message.content)
    return deepcopy(blocks)


def mechanical_digest(head: Sequence[AnyMessage]) -> str:
    """A summary made without a model: what was called and how each call ended."""
    results = {m.tool_call_id: m for m in head if isinstance(m, ToolMessage)}
    earlier = ""
    lines: list[str] = []
    for message in head:
        if _is_summary(message):
            earlier = str(message.additional_kwargs.get("nova_summary") or "")
        elif isinstance(message, AIMessage):
            said = _text(message).strip()
            if said:
                lines.append("- said: " + _one_line(said, 300))
            for call in message.tool_calls or []:
                result = results.get(call.get("id"))
                outcome = ""
                if result is not None:
                    failed = getattr(result, "status", None) == "error"
                    outcome = (" → error: " if failed else " → ") + _one_line(_text(result), 160)
                lines.append("- " + _render_tool_call(call) + outcome)
    omitted = max(0, len(lines) - DIGEST_LINES)
    parts = [
        "## Conversation Summary",
        "[The summary model was unavailable, so this is a mechanical record of the "
        "earlier work, not an interpretation of it.]",
    ]
    if earlier:
        parts.append("### Earlier summary\n" + earlier)
    if lines:
        heading = "### Actions taken, oldest first"
        if omitted:
            heading += f" ({omitted} earlier ones not listed)"
        parts.append(heading + "\n" + "\n".join(lines[-DIGEST_LINES:]))
    return "\n\n".join(parts)


def _transcript(head: Sequence[AnyMessage]) -> str:
    """``head`` in full, for the saved file. # ponytail: text only, images are not archived."""
    parts: list[str] = []
    for message in head:
        if _is_summary(message):
            continue
        if isinstance(message, ToolMessage):
            parts.append(f"### tool result ({message.name or 'tool'})\n{_text(message)}")
        elif isinstance(message, AIMessage):
            calls = "\n".join(
                f"-> {call.get('name', 'tool')}("
                f"{json.dumps(call.get('args') or {}, ensure_ascii=False, default=str)})"
                for call in message.tool_calls or []
            )
            parts.append("### assistant\n" + "\n".join(filter(None, [_text(message), calls])))
        else:
            parts.append(f"### {message.type}\n{_text(message)}")
    return "\n\n".join(parts) + "\n"


def _run_sync(coroutine: Awaitable[Any]) -> Any:  # noqa: ANN401
    """Run ``coroutine`` to completion from synchronous code, loop or no loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()  # type: ignore[arg-type]


def _announce() -> None:
    """Tell the UI a summary is being written; that call itself does not stream."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({"type": "nova_compaction_start"})
    except Exception:  # noqa: BLE001 - outside a graph run there is no stream
        pass


def _is_overflow(error: BaseException) -> bool:
    from langchain_core.exceptions import ContextOverflowError

    from novacode_cli.errors import is_context_overflow

    return isinstance(error, ContextOverflowError) or is_context_overflow(error)


def _model_window(model: object) -> int:
    profile = getattr(model, "profile", None)
    if isinstance(profile, Mapping):
        window = profile.get("max_input_tokens")
        if isinstance(window, int) and window > 0:
            # The profile is the model's real window; a subagent on its own
            # model works within the same budget as the main agent.
            from novacode_cli.context._analysis import configured_budget, working_window

            return working_window(window, configured_budget())
    return _window_tokens(model)  # type: ignore[arg-type]


class ContextCompactionMiddleware(AgentMiddleware):
    """Prune, trim and summarize the conversation to fit a context budget.

    Args:
        model: The model that writes summaries. ``None`` uses whichever model
            the request is for, which is what a subagent on its own model needs.
        backend: Where the replaced messages are saved (``conversation_history/``).
            ``None`` saves nothing; the summary then names no file.
        context_window: The window to budget against, in tokens. 0 reads it from
            the request's model. Pass Nova's ``context_budget_tokens`` here to
            work within less than the model allows.
        prune_edit: The tool-result clearing edit
            (``OffloadingToolUsesEdit`` or ``VerdictToolUsesEdit``); ``None``
            skips stage 1.
        scorer: A ``VerdictScorer`` to hand each turn's messages to, when tool
            verdicts are enabled. It scores on a background thread.
    """

    state_schema = CompactionState

    def __init__(
        self,
        model: Any = None,  # noqa: ANN401 - a chat model, or None
        *,
        backend: Any = None,  # noqa: ANN401 - a deepagents backend
        context_window: int = 0,
        prune_edit: Any = None,  # noqa: ANN401 - a ContextEdit
        scorer: Any = None,  # noqa: ANN401 - a VerdictScorer
    ) -> None:
        """Configure the stages; see the class docstring."""
        super().__init__()
        self._model = model
        self._backend = backend
        self._window = int(context_window or 0)
        self._prune_edit = prune_edit
        self._scorer = scorer
        self._limits: dict[int, Limits] = {}
        # believed window -> the size a provider actually refused. Nova's idea
        # of a window can be wrong (an Ollama num_ctx, a proxy's own limit).
        self._refusals = {}
        self._refused = {}
        self._tool_tokens: dict[tuple[str, ...], int] = {}
        # Provider-reported input tokens over this middleware's own estimate.
        # ponytail: one running value per instance; per-model if models are mixed.
        self._scales = {}
        self._scale = 1.0
        root = str(getattr(backend, "artifacts_root", "/") or "/").rstrip("/")
        self._history_dir = f"{root}/conversation_history"

    @property
    def name(self) -> str:
        """The library summarizer's name, so this replaces it wherever it is built in.

        ``create_deep_agent`` adds its own summarizer to every stack and swaps
        in a custom middleware only when the names match.
        """
        return "SummarizationMiddleware"

    # ── sizing ──────────────────────────────────────────────────────────
    def limits(self, model: object) -> Limits:
        """The thresholds in force for a request to ``model``."""
        believed = self._window or _model_window(model)
        window = min(believed, self._refused.get(believed, believed))
        found = self._limits.get(window)
        if found is None:
            # The edit's own trigger was sized for the configured window.
            prune = getattr(self._prune_edit, "trigger", None) if window == self._window else None
            found = self._limits[window] = limits_for(window, prune=prune)
        return found

    def _learn_refusal(self, model: object, tokens: int) -> tuple[int, int | None]:
        """A request of ``tokens`` was refused as too long: that is the window.

        Returns what :meth:`_unlearn` needs to take it back.
        """
        believed = self._window or _model_window(model)
        previous = self._refused.get(believed)
        self._refused[believed] = max(1_000, min(tokens, previous or believed))
        return believed, previous

    def _unlearn(self, learned: tuple[int, int | None]) -> None:
        """The shrunk retry failed too, so size was not the problem: forget it.

        Otherwise one misread error on a small request would leave the rest of
        the session compacting against a window of a few thousand tokens.
        """
        believed, previous = learned
        if previous is None:
            self._refused.pop(believed, None)
        else:
            self._refused[believed] = previous

    def _overhead(self, request: ModelRequest) -> int:
        """Tokens the system prompt and tool schemas add to every request."""
        system = request.system_message
        tokens = _request_size_tokens([system]) if system is not None else 0
        tools = list(request.tools or [])
        if tools:
            from langchain_core.utils.function_calling import convert_to_openai_tool

            key = tuple(
                json.dumps(convert_to_openai_tool(tool), sort_keys=True, default=str)
                for tool in tools
            )
            if key not in self._tool_tokens:
                try:
                    self._tool_tokens[key] = count_tokens_approximately([], tools=tools)
                except Exception:  # noqa: BLE001 - an odd tool object is not worth a turn
                    self._tool_tokens[key] = 0
            tokens += self._tool_tokens[key]
        return tokens

    @property
    def _refused(self) -> dict[int, int]:
        from novacode_cli.session.runtime_identity import current_identity
        identity = current_identity.get()
        return self._refusals.setdefault(identity.session if identity else "unbound", {})

    @_refused.setter
    def _refused(self, value: dict[int, int]) -> None:
        from novacode_cli.session.runtime_identity import current_identity
        identity = current_identity.get()
        self._refusals[identity.session if identity else "unbound"] = value

    @property
    def _scale(self) -> float:
        from novacode_cli.session.runtime_identity import current_identity
        identity = current_identity.get()
        return self._scales.get(identity.session if identity else "unbound", 1.0)

    @_scale.setter
    def _scale(self, value: float) -> None:
        from novacode_cli.session.runtime_identity import current_identity
        identity = current_identity.get()
        self._scales[identity.session if identity else "unbound"] = value

    def _calibrate(self, response: Any, raw_tokens: int) -> None:  # noqa: ANN401
        """Learn how far the estimate was from what the provider counted."""
        response = getattr(response, "model_response", response)
        results = (
            [response] if isinstance(response, AIMessage) else getattr(response, "result", None)
        )
        reply = next((m for m in reversed(results or []) if isinstance(m, AIMessage)), None)
        seen = ((getattr(reply, "usage_metadata", None) or {}).get("input_tokens")) or 0
        if seen > 0 and raw_tokens > 0:
            ratio = min(2.0, max(0.9, seen / raw_tokens))
            self._scale = (self._scale + ratio) / 2

    # ── planning: stages 1 and 2, and whether stage 3 is due ────────────
    def _plan(self, request: ModelRequest, *, force: bool = False) -> _Plan:
        state = request.state or {}
        messages, prior = effective_view(request.messages, state.get("_summarization_event"))
        limits = self.limits(request.model)
        from novacode_cli.context.snapshot import ContextSnapshot

        snapshot = ContextSnapshot.capture(request)
        snapshot.validate(limits.window)
        if snapshot.output_reservation:
            usable = max(1, limits.window - snapshot.output_reservation)
            limits = Limits(
                limits.window,
                min(limits.trigger, usable),
                min(limits.prune, usable),
                min(limits.keep, usable),
            )
        overhead = self._overhead(request)

        def raw(view: Sequence[AnyMessage]) -> int:
            return _request_size_tokens(view) + overhead

        def count(view: Sequence[AnyMessage]) -> int:
            return int(raw(view) * self._scale)

        if self._scorer is not None:
            try:
                self._scorer.maybe_score(messages)
            except Exception:  # noqa: BLE001 - scoring never blocks or fails a turn
                logger.debug("Could not queue a scoring pass", exc_info=True)

        view = messages
        if force or count(messages) >= limits.prune:
            if self._prune_edit is not None:
                pruned = list(messages)
                try:
                    # Whether to prune is decided above; the edit's own trigger
                    # may be sized for a window larger than the one in force.
                    self._prune_edit.apply(pruned, count_tokens=lambda _view: 10**12)
                    view = pruned
                except Exception:  # noqa: BLE001 - an unpruned turn beats a failed one
                    logger.warning(
                        "Tool-result pruning failed; sending them as they are", exc_info=True
                    )
            view = trim_old_arguments(view, tail_start(view, limits.keep))

        raw_tokens = raw(view)
        cutoff = 0
        if force or int(raw_tokens * self._scale) >= limits.trigger:
            cutoff = tail_start(view, limits.keep)
            body = view[1:cutoff] if prior is not None else view[:cutoff]
            # Not worth a model call unless it removes a real share of the context.
            worthwhile = len(body) >= 2 and count(body) - int(overhead * self._scale) >= max(
                1_000, limits.trigger // 10
            )
            if not body or not (force or worthwhile):
                cutoff = 0
        return _Plan(view, prior, cutoff, limits, raw_tokens)

    # ── stage 3 ─────────────────────────────────────────────────────────
    async def _summarize(self, model: Any, head: list[AnyMessage], window: int) -> str:  # noqa: ANN401
        """A model-written summary of ``head``. Raises when none can be had."""
        # `nostream`: LangGraph would otherwise stream this call's tokens to the
        # UI as if they were the assistant's reply.
        quiet = model.with_config(tags=["nostream"], metadata={"lc_source": SUMMARY_SOURCE})
        return await summarize_conversation(quiet, head, context_window=window, tail_kept=True)

    async def _archive(self, head: list[AnyMessage]) -> str | None:
        """Save the messages being replaced; the path, or ``None`` if it failed."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = f"{self._history_dir}/{stamp}-{uuid.uuid4().hex[:8]}.md"
        try:
            if self._backend is not None:
                result = await self._backend.awrite(path, _transcript(head))
                if result is None or getattr(result, "error", None):
                    logger.warning("Could not save replaced messages to %s", path)
                    return None
        except Exception:  # noqa: BLE001 - the summary is still worth having
            logger.warning("Could not save replaced messages to %s", path, exc_info=True)
            return None
        try:
            from novacode_cli.context.archives import default_recovery_store
            from novacode_cli.session.runtime_identity import current_identity
            identity = current_identity.get()
            manifest = await asyncio.to_thread(
                default_recovery_store().archive, identity.thread if identity else "unbound",
                list(head), source="automatic",
            )
            # Keep the readable backend transcript as a compatibility reference;
            # its companion manifest is the authoritative recovery format.
            if self._backend is None:
                return str(manifest)
        except Exception:
            logger.warning("Full-message archive publication failed", exc_info=True)
            return None
        return path

    def _summary_message(
        self, head: list[AnyMessage], summary: str, path: str | None, limits: Limits,
        todos: object = None,
    ) -> HumanMessage:
        from novacode_cli.context.task_progress import render_task_progress, task_progress

        progress = task_progress(head, todos)
        said = user_messages(head, budget_chars=limits.keep * 2)
        parts = [_continuation_framing(tail_kept=True)]
        if said:
            parts.append(_historical_user_requests(said))
        parts.append(summary)
        parts.append(render_task_progress(progress))
        parts.append(_rehydration_note())
        files = [
            f
            for m in head
            if _is_summary(m)
            for f in m.additional_kwargs.get("history_files") or []
        ]
        if path:
            files.append(path)
        if files:
            parts.append(
                "The messages this replaces are saved in full, oldest first, at: "
                + ", ".join(files)
                + ". read_file one if an exact detail is needed."
            )
        body = "\n\n".join(filter(None, parts))
        blocks = user_content_blocks(head)
        content = f"{COMPACTION_SUMMARY_MARKER}\n\n{body}"
        return HumanMessage(
            content=[{"type": "text", "text": content}, *blocks] if blocks else content,
            additional_kwargs={
                "lc_source": SUMMARY_SOURCE,
                "nova_summary": summary,
                "nova_task_progress": progress,
                "user_messages": said,
                "history_files": files,
                "user_content_blocks": blocks,
            },
        )

    async def _compact(
        self, request: ModelRequest, plan: _Plan, *, overflow: bool
    ) -> tuple[list[AnyMessage], dict[str, Any] | None]:
        """Stage 3. Returns the messages to send and the state update, if any."""
        state = request.state or {}
        backoff = state.get("_compaction_backoff") or {}
        from novacode_cli.context.snapshot import ContextSnapshot

        source_generation = ContextSnapshot.capture(request).generation
        failures = int(backoff.get("failures") or 0)
        waiting = len(request.messages) < int(backoff.get("retry_at") or 0)
        head, tail = plan.messages[: plan.cutoff], plan.messages[plan.cutoff :]
        if overflow:
            tail = _clip_results(tail)

        summary = None
        if not waiting:
            _announce()
            try:
                summary = await self._summarize(
                    self._model or request.model, head, plan.limits.window
                )
            except Exception as error:  # noqa: BLE001 - a failed summary never fails the turn
                failures += 1
                logger.warning("Context summary failed (attempt %d): %s", failures, error)
        if summary is None:
            if not overflow and failures < FAILURES_BEFORE_DIGEST:
                update = None
                if not waiting:
                    retry_at = len(request.messages) + 4 * failures
                    update = {"_compaction_backoff": {"failures": failures, "retry_at": retry_at}}
                return plan.messages, update
            summary = mechanical_digest(head)

        source_messages = (request.state or {}).get("messages") or head
        path = await self._archive(list(source_messages))
        if path is None:
            logger.warning("Compaction preparation discarded because archival failed")
            return list(request.messages), None
        if ContextSnapshot.capture(request).generation != source_generation:
            logger.info("Discarded superseded automatic compaction")
            return list(request.messages), None
        message = self._summary_message(head, summary, path, plan.limits, state.get("todos"))
        cutoff = plan.cutoff if plan.prior_cutoff is None else plan.prior_cutoff + plan.cutoff - 1
        logger.info(
            "Compacted %d messages into a summary (~%d tokens before)",
            len(head),
            int(plan.raw_tokens * self._scale),
        )
        return [message, *tail], {
            "_summarization_event": {
                "cutoff_index": cutoff,
                "summary_message": message,
                "file_path": path,
                "source_generation": source_generation,
            },
            "_compaction_backoff": None,
        }

    # ── the model call ──────────────────────────────────────────────────
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Shrink the request as far as needed, then make the call."""
        # Off the loop the UI shares: the first pruning pass writes every
        # cleared result to disk (1.4 s for 600 of them), and even a warm
        # pass over a long conversation is tens of milliseconds.
        plan = await asyncio.to_thread(self._plan, request)
        learned = None
        if not plan.cutoff:
            try:
                response = await handler(request.override(messages=plan.messages))
            except Exception as error:
                if not _is_overflow(error):
                    raise
                # The provider knows the real limit; whatever we estimated, cut now.
                learned = self._learn_refusal(request.model, int(plan.raw_tokens * self._scale))
                plan = await asyncio.to_thread(self._plan, request, force=True)
            else:
                self._calibrate(response, plan.raw_tokens)
                return response

        if plan.cutoff:
            messages, update = await self._compact(request, plan, overflow=learned is not None)
        else:  # too long, and nothing before the tail to summarize
            messages, update = _clip_results(plan.messages), None
        try:
            response = await handler(request.override(messages=messages))
        except Exception as error:
            if learned is not None:
                self._unlearn(learned)
                raise
            if not _is_overflow(error):
                raise
            # Summarized on schedule and still refused: the kept tail holds a
            # result that is too large on its own.
            response = await handler(request.override(messages=_clip_results(messages)))
        if update is None:
            return response
        from novacode_cli.context.snapshot import ContextSnapshot
        source = (update.get("_summarization_event") or {}).get("source_generation")
        if source and ContextSnapshot.capture(request).generation != source:
            return response
        return ExtendedModelResponse(model_response=response, command=Command(update=update))

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Synchronous twin of :meth:`awrap_model_call`."""
        plan = self._plan(request)
        learned = None
        if not plan.cutoff:
            try:
                response = handler(request.override(messages=plan.messages))
            except Exception as error:
                if not _is_overflow(error):
                    raise
                # The provider knows the real limit; whatever we estimated, cut now.
                learned = self._learn_refusal(request.model, int(plan.raw_tokens * self._scale))
                plan = self._plan(request, force=True)
            else:
                self._calibrate(response, plan.raw_tokens)
                return response

        if plan.cutoff:
            messages, update = _run_sync(self._compact(request, plan, overflow=learned is not None))
        else:  # too long, and nothing before the tail to summarize
            messages, update = _clip_results(plan.messages), None
        try:
            response = handler(request.override(messages=messages))
        except Exception as error:
            if learned is not None:
                self._unlearn(learned)
                raise
            if not _is_overflow(error):
                raise
            # Summarized on schedule and still refused: the kept tail holds a
            # result that is too large on its own.
            response = handler(request.override(messages=_clip_results(messages)))
        if update is None:
            return response
        from novacode_cli.context.snapshot import ContextSnapshot
        source = (update.get("_summarization_event") or {}).get("source_generation")
        if source and ContextSnapshot.capture(request).generation != source:
            return response
        return ExtendedModelResponse(model_response=response, command=Command(update=update))


__all__ = [
    "CompactionState",
    "ContextCompactionMiddleware",
    "Limits",
    "effective_view",
    "limits_for",
    "mechanical_digest",
    "tail_start",
    "trim_old_arguments",
    "user_messages",
]
