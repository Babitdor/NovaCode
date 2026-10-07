"""Which old tool results are stale enough to clear.

Nova already clears tool results **restorably**: ``OffloadingToolUsesEdit``
(``novacode_cli/agents/tool_offload.py``) writes the payload to
``<agent_dir>/cleared/`` and leaves a placeholder naming the file, so clearing
costs context but never information. What that edit has no opinion about is
*which* results to clear: it takes everything older than a token trigger except
the most recent few.

This module supplies that opinion. It is a port of the decision-model half of
``fast-jev-compaction``: build a small skeleton of the conversation, ask a
Jev-shaped decision model whether one old tool result is still needed, and clear
the ones it scores stale. Nothing else changes -- text is never rewritten, and
recovery through ``/cleared/`` is untouched.

Two deliberate departures from the original library:

* **One question per result, not two.** The library also asks whether the tool
  *call* should stay, because it is free to delete a call together with its
  result. Nova is not: providers require every ``tool_call`` on an ``AIMessage``
  to be answered by a matching ``ToolMessage``, so the call always stays and the
  answer could never be acted on. Dropping the question halves the questions per
  request, which matters against a 64-question ceiling.
* **``stale`` is advice, not an action.** This module decides; the caller clears
  (and therefore offloads). Keeping the two apart is what lets the selection be
  measured against the heuristic it would replace.

The endpoint is Ollama's Jev-shaped ``/v1/systemone`` (Tev1 by default). It
scores every question against the full state in a single prompt and runs with a
context of about 2,000 tokens, which is why the state defaults are small.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)

if TYPE_CHECKING:
    from novacode_cli.config.nova_config import NovaConfig

logger = logging.getLogger(__name__)

# ── defaults ────────────────────────────────────────────────────────────────
# Sized for a Jev-shaped decision model on a ~2,000-token context, and sized
# *together*: state and window have to agree, or the model never gets asked.
# Measured on 105 real archived sessions (scripts/probe_window_capacity.py):
#   state  1,000 tokens -> the largest window that fits is  8-16 results
#   state  1,800 tokens -> the largest window that fits is 16-24 results
# So a 1,400-token state with a 12-result window is comfortably inside the
# model's context and leaves ~380 tokens for the questions themselves.
DEFAULT_MAX_STATE_TOKENS = 1_400
DEFAULT_MAX_REQUEST_TOKENS = 1_800

#: Minimum probability that a result is still needed for it to survive.
DEFAULT_KEEP_THRESHOLD = 0.5

#: How many of the most recent results a verdict may be asked about.
#:
#: Measured against 105 real archived sessions (``scripts/probe_window_capacity``):
#: the *whole*-session skeleton does not fit this model's context at all -- a
#: 308-result session needs ~135,000 tokens even after every shrink stage, against
#: a budget of ~1,400. Only a bounded window fits, and the largest that fits at a
#: 1,400-token state is 12-16 results. 12 is chosen for headroom, and it has to
#: cover the ``keep`` results the edit pins anyway, so it leaves about seven
#: results a verdict can actually act on.
#:
#: Results older than the window are **not** judged, and an unjudged result is
#: kept rather than cleared (``VerdictToolUsesEdit`` fails open). So once the
#: reducer is enabled, the heuristic's clearing only applies *within* this
#: window: a result the model was never asked about stays verbatim instead of
#: being cleared on age alone. Verdicts never make an older result's fate worse,
#: but they do mean far fewer results are cleared in total.
MAX_VERDICT_WINDOW = 12

#: Model name at the endpoint.
DEFAULT_MODEL = "tev1"

#: Where a decision request goes when no endpoint is configured.
DEFAULT_HOST = "http://localhost:11434"
SYSTEM_ONE_PATH = "/v1/systemone"

#: The endpoint answers at most this many questions per request.
MAX_QUESTIONS_PER_REQUEST = 64

#: Tokens the request envelope (model name, question keys) adds around state.
REQUEST_OVERHEAD_TOKENS = 20

#: The ``state.context`` prose. Kept verbatim from the original library: the
#: model was trained against this kind of framing.
STATE_CONTEXT = (
    "A coding assistant conversation is being compacted to free context. "
    "`history` is the whole conversation so far, oldest first; tool outputs are "
    "replaced by a short `result` note and long texts may be abridged. Each "
    "question asks whether the full output of one tool call still needs to stay "
    "in the history verbatim. Whatever is not kept is deleted permanently, but "
    "the assistant can always re-run a tool or re-read a file."
)

#: Successive caps on the serialised tool input included per call.
INPUT_CHARS = (1000, 200, 60)

#: Head/tail budget when a long text has to be abridged.
TEXT_HEAD = 400
TEXT_TAIL = 150

_TOKEN_PIECES = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]")

#: Whitespace runs, for collapsing a tool input onto one line. Kept at module
#: level so the f-string using it stays legal: this project is pinned to Python
#: 3.11, where an f-string expression may not contain a backslash.
_WHITESPACE = re.compile(r"\s+")


# ── token estimation ────────────────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """Estimate tokens without a tokenizer.

    A word costs one token per six letters, a digit half a token, any other
    symbol nine tenths. Calibrated against the usage Jev reports for real
    transcripts, where it lands 2-18% above the true count; a plain
    characters-per-token ratio undercounts JSON-heavy states by up to 40%.

    The calibration has **not** been checked against Tev1, so treat the result
    as a budget guard rather than a measurement.
    """
    tokens = 0.0
    for piece in _TOKEN_PIECES.findall(text):
        first = piece[0]
        if "0" <= first <= "9":
            tokens += len(piece) / 2
        elif "A" <= first <= "Z" or "a" <= first <= "z":
            tokens += 1 + (len(piece) - 1) // 6
        else:
            tokens += 0.9
    return math.ceil(tokens)


def truncate(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, ellipsis included in the limit."""
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}\u2026"


def _abridge(text: str, head: int, tail: int) -> str:
    """Head plus tail with an omitted-marker between them."""
    if len(text) <= head + tail + 40:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[\u2026 {omitted} chars omitted \u2026]\n{text[-tail:]}"


def _dumps(value: Any) -> str:
    """Serialise the way ``JSON.stringify`` does: no spaces, raw UTF-8."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _format_content(content: Any) -> str:
    """Render message content to text.

    Imported from :mod:`novacode_cli.compaction` on purpose: the state has to
    read the same whether it is built here or summarised there.
    """
    from novacode_cli.compaction import _format_message_content

    return _format_message_content(content)


# ── candidates ──────────────────────────────────────────────────────────────
@dataclass
class VerdictCandidate:
    """One tool result that could be cleared, with the call that produced it."""

    #: The originating ``tool_call_id``. Stable within a session.
    call_id: str
    #: Short name used in the state and the question keys (``t1``, ``t2``, ...).
    label: str
    tool: str
    args: dict[str, Any]
    content_chars: int
    is_error: bool
    #: Inside the newest ``preserve_recent_messages`` results; never a candidate.
    pinned: bool
    #: Message index of the ``AIMessage`` holding the call.
    call_index: int = 0
    #: The result's real content.
    #:
    #: This exists because the verdict cache is keyed on the content
    #: (:func:`result_key`): storing a verdict against a placeholder would put it
    #: under a key no real ``ToolMessage`` can ever produce, so every later
    #: lookup would miss and every verdict would be silently ignored. Measured
    #: cost of getting this wrong: 0 of 149 results could be answered for, at
    #: every threshold.
    content: str = ""


@dataclass
class Verdict:
    """What the model answered about one candidate."""

    call_id: str
    #: Probability the full result is still needed, from the ``noul`` answer.
    noul: float

    @property
    def stale(self) -> bool:
        """True when the result scored below the keep threshold."""
        return self.noul < DEFAULT_KEEP_THRESHOLD


def _call_args(message: AIMessage) -> Iterable[dict[str, Any]]:
    return message.tool_calls or []


def collect_candidates(
    messages: Sequence[AnyMessage],
    *,
    preserve_recent_results: int = 5,
    window: int | None = MAX_VERDICT_WINDOW,
) -> list[VerdictCandidate]:
    """Pair every tool result with its call, newest ``N`` results pinned.

    A result with no matching ``tool_call_id`` on any assistant message is not a
    candidate: there is no call to describe it in the state, and clearing it
    would leave nothing to re-derive it from.

    ``window`` caps how far back a verdict may be asked about, because the state
    for a whole long session does not fit the model's context at all (see
    :data:`MAX_VERDICT_WINDOW`). Results older than the window are simply absent
    from the returned list, so no verdict is ever asked about them -- and since
    an unjudged result is kept rather than cleared, they stay verbatim.
    """
    calls: dict[str, tuple[str, dict[str, Any], int]] = {}
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in _call_args(message):
                call_id = call.get("id")
                if call_id:
                    calls[call_id] = (
                        str(call.get("name") or "tool"),
                        call.get("args") or {},
                        index,
                    )

    found: list[tuple[int, VerdictCandidate]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            continue
        origin = calls.get(message.tool_call_id)
        if origin is None:
            continue
        tool, args, call_index = origin
        content = _format_content(message.content)
        found.append(
            (
                index,
                VerdictCandidate(
                    call_id=message.tool_call_id,
                    label="",
                    tool=tool,
                    args=dict(args),
                    content_chars=len(content),
                    is_error=bool(getattr(message, "status", None) == "error"),
                    pinned=False,
                    call_index=call_index,
                    content=content,
                ),
            )
        )

    # Keep only the newest `window` results; labels are assigned after the cut so
    # they stay a compact t1..tN in the state and the question names.
    total = len(found)
    if window is not None:
        found = found[max(0, total - window) :]
    pinned_from = max(0, len(found) - max(0, preserve_recent_results))

    candidates: list[VerdictCandidate] = []
    for position, (_, candidate) in enumerate(found):
        candidate.pinned = position >= pinned_from
        candidate.label = f"t{position + 1}"
        candidates.append(candidate)
    return candidates


# ── questions ───────────────────────────────────────────────────────────────
def questions_for(candidate: VerdictCandidate) -> dict[str, dict[str, Any]]:
    """The one ``noul`` question asked about a result.

    ``noul`` is the probability the answer is true, so the question is phrased
    as the statement that must hold for the result to be worth keeping.
    """
    return {
        f"result_{candidate.label}": {
            "type": "noul",
            "instructions": (
                f"The full output of tool call {candidate.label} "
                f"({candidate.tool}, {candidate.content_chars} chars) should stay "
                "in the history verbatim: the assistant still needs its contents "
                "and re-running the tool would not do"
            ),
        }
    }


def batch_candidates(
    candidates: Sequence[VerdictCandidate],
    state_tokens: int,
    *,
    max_request_tokens: int = DEFAULT_MAX_REQUEST_TOKENS,
) -> list[list[VerdictCandidate]]:
    """Split candidates into batches that fit one request.

    Two ceilings, both the endpoint's: the request's token budget (the state is
    resent with every batch) and :data:`MAX_QUESTIONS_PER_REQUEST`.
    """
    budget = max_request_tokens - state_tokens - REQUEST_OVERHEAD_TOKENS
    batches: list[list[VerdictCandidate]] = []
    current: list[VerdictCandidate] = []
    current_tokens = 0
    current_questions = 0
    for candidate in candidates:
        questions = questions_for(candidate)
        asked = len(questions)
        tokens = estimate_tokens(_dumps(questions))
        if current and (
            current_questions + asked > MAX_QUESTIONS_PER_REQUEST
            or current_tokens + tokens > budget
        ):
            batches.append(current)
            current = []
            current_tokens = 0
            current_questions = 0
        if not current and tokens > budget:
            raise ValueError(
                "state leaves no room for questions "
                f"(~{state_tokens} of {max_request_tokens} tokens)"
            )
        current.append(candidate)
        current_tokens += tokens
        current_questions += asked
    if current:
        batches.append(current)
    return batches


# ── state ───────────────────────────────────────────────────────────────────
@dataclass
class FittedState:
    """A conversation skeleton that fits the token budget."""

    state: dict[str, Any]
    tokens: int
    #: Which fitting stage produced it, for diagnostics.
    stage: str


def goal_from_messages(messages: Sequence[AnyMessage]) -> str:
    """The last three user prompts, as the default ``goal``."""
    prompts = [
        truncate(_format_content(message.content), 500)
        for message in messages
        if isinstance(message, HumanMessage) and _format_content(message.content).strip()
    ]
    return "\n".join(prompts[-3:])


def _input_text(args: dict[str, Any], limit: int) -> str:
    try:
        encoded = _dumps(args)
    except (TypeError, ValueError):
        encoded = "[unserializable input]"
    return truncate(encoded, limit)


def _compact_call(candidate: VerdictCandidate) -> str:
    """One call as a single line, for when the structured form is too costly."""
    pairs = []
    for key, value in candidate.args.items():
        text = value if isinstance(value, str) else _input_text({key: value}, 200)
        pairs.append(f"{key}={_WHITESPACE.sub(' ', str(text))}")
    joined = " ".join(pairs)
    outcome = "error" if candidate.is_error else "ok"
    return (
        f"{candidate.label} {candidate.tool} {truncate(joined, INPUT_CHARS[2])}"
        f" \u2192 {outcome} {candidate.content_chars}ch"
    )


def _is_pinned(index: int, total: int, preserve_recent_messages: int) -> bool:
    return index == 0 or index >= total - preserve_recent_messages


def _history_entries(
    messages: Sequence[AnyMessage],
    candidates: Sequence[VerdictCandidate],
    input_chars: int,
) -> list[dict[str, Any]]:
    """The conversation skeleton: results replaced by a one-line note.

    System messages are left out: the system prompt is not part of the
    conversation, and it is large enough to crowd out everything else.
    """
    by_call: dict[int, list[VerdictCandidate]] = {}
    for candidate in candidates:
        by_call.setdefault(candidate.call_index, []).append(candidate)

    entries: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            text = _format_content(message.content)
            if text.strip():
                entries.append({"i": index, "role": "user", "text": text})
        elif isinstance(message, AIMessage):
            text = _format_content(message.content)
            calls = by_call.get(index)
            if not text.strip() and not calls:
                continue
            entry: dict[str, Any] = {"i": index, "role": "assistant", "text": text}
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": candidate.label,
                        "tool": candidate.tool,
                        "input": _input_text(candidate.args, input_chars),
                        "result": (
                            f"{'error' if candidate.is_error else 'ok'}, "
                            f"{candidate.content_chars} chars (omitted)"
                        ),
                    }
                    for candidate in calls
                ]
            entries.append(entry)
    return entries


def _merge_call_runs(
    history: list[dict[str, Any]],
    pinned: Any,
) -> list[dict[str, Any]]:
    """Fold runs of adjacent call-only entries, paying one envelope per run."""
    merged: list[dict[str, Any]] = []

    def foldable(entry: dict[str, Any]) -> bool:
        calls = entry.get("tool_calls")
        return (
            not pinned(entry)
            and not entry.get("text", "").strip()
            and bool(calls)
            and isinstance(calls[0], str)
        )

    for entry in history:
        previous = merged[-1] if merged else None
        if previous is not None and foldable(previous) and foldable(entry):
            previous["tool_calls"] = [*previous["tool_calls"], *entry["tool_calls"]]
            continue
        merged.append(dict(entry))
    return merged


def build_verdict_state(
    messages: Sequence[AnyMessage],
    candidates: Sequence[VerdictCandidate],
    *,
    max_state_tokens: int = DEFAULT_MAX_STATE_TOKENS,
    preserve_recent_messages: int = 6,
    goal: str | None = None,
) -> FittedState:
    """Build the state for one request and shrink it until it fits.

    Stages, each applied only if the previous was not enough: tool inputs
    truncated to 1000, then 200, then 60 characters; long texts abridged to head
    plus tail, oldest non-pinned first; old non-pinned messages collapsed to an
    omitted-note; old tool calls reduced to one line each; old call-less messages
    left out; runs of call-only messages folded together. Raises when even that
    is too big -- the caller must then keep the history as it is rather than
    guess.
    """
    resolved_goal = goal if goal is not None else goal_from_messages(messages)

    def state_of(history: list[dict[str, Any]]) -> dict[str, Any]:
        return {"context": STATE_CONTEXT, "goal": resolved_goal, "history": history}

    def entry_tokens(entry: dict[str, Any]) -> int:
        return estimate_tokens(_dumps(entry)) + 1

    base_tokens = estimate_tokens(_dumps(state_of([])))
    total_messages = len(messages)

    def pinned_entry(entry: dict[str, Any]) -> bool:
        return _is_pinned(entry["i"], total_messages, preserve_recent_messages)

    def fitted(history: list[dict[str, Any]], stage: str) -> FittedState:
        tokens = base_tokens + sum(entry_tokens(entry) for entry in history)
        return FittedState(state=state_of(history), tokens=tokens, stage=stage)

    def fits(history: list[dict[str, Any]]) -> bool:
        return base_tokens + sum(entry_tokens(entry) for entry in history) <= max_state_tokens

    for limit in INPUT_CHARS:
        history = _history_entries(messages, candidates, limit)
        if fits(history):
            return fitted(history, "full" if limit == INPUT_CHARS[0] else f"inputs<={limit}")

    history = _history_entries(messages, candidates, INPUT_CHARS[-1])
    # Oldest-first, pinned last, so the newest messages are the last to lose text.
    order = sorted(range(len(history)), key=lambda index: pinned_entry(history[index]))

    for index in order:
        entry = history[index]
        if len(entry.get("text", "")) <= TEXT_HEAD + TEXT_TAIL + 40:
            continue
        entry["text"] = _abridge(entry["text"], TEXT_HEAD, TEXT_TAIL)
        if fits(history):
            return fitted(history, "texts abridged")

    for index in order:
        entry = history[index]
        if pinned_entry(entry) or not entry.get("text", ""):
            continue
        original = len(_format_content(messages[entry["i"]].content))
        entry["text"] = f"[\u2026 {original} chars omitted \u2026]"
        if fits(history):
            return fitted(history, "old messages collapsed")

    by_message: dict[int, list[VerdictCandidate]] = {}
    for candidate in candidates:
        by_message.setdefault(candidate.call_index, []).append(candidate)

    for index in order:
        entry = history[index]
        own = by_message.get(entry["i"])
        if pinned_entry(entry) or not own:
            continue
        entry["tool_calls"] = [_compact_call(candidate) for candidate in own]
        if fits(history):
            return fitted(history, "old calls compacted")

    # Dropping entries shifts every later index, so the removals are collected
    # into a set first and filtered once at the end -- the same shape as the
    # library, and the reason it cannot be a `del` in a loop.
    left_out: set[int] = set()
    for index in order:
        entry = history[index]
        if pinned_entry(entry) or entry.get("tool_calls"):
            continue
        left_out.add(index)
        remaining = [item for position, item in enumerate(history) if position not in left_out]
        if fits(remaining):
            return fitted(remaining, "old messages left out")

    remaining = [item for position, item in enumerate(history) if position not in left_out]
    merged = _merge_call_runs(remaining, pinned_entry)
    if fits(merged):
        return fitted(merged, "old calls merged")

    tokens = base_tokens + sum(entry_tokens(entry) for entry in merged)
    raise ValueError(
        f"history too large for the decision model (~{tokens} tokens after "
        f"truncation, limit {max_state_tokens})"
    )


def stale_from_noul(noul: float, *, keep_threshold: float = DEFAULT_KEEP_THRESHOLD) -> bool:
    """True when the result should be cleared: it scored below the threshold."""
    return noul < keep_threshold


# ── the endpoint ────────────────────────────────────────────────────────────
class DecisionClient(Protocol):
    """Anything that can answer verdict questions."""

    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, float]:  # pragma: no cover - protocol
        """Return ``{question_name: noul_probability}``."""
        ...


def parse_answers(payload: Any) -> dict[str, float]:
    """Pull ``answers[name].noul`` out of a System One response.

    Raises rather than guessing: a missing or non-numeric answer must surface as
    "no verdict" so the caller keeps the result. ``TypeError`` marks a shape the
    response cannot have (not an object, an answer that is not an object, a
    ``noul`` that is not a number); ``ValueError`` marks a well-formed number
    that is not usable (infinite). Callers that only want to know "did I get a
    verdict" catch both, but the distinction is there for a caller that wants to
    report a malformed endpoint separately from a bad value.
    """
    if not isinstance(payload, dict):
        raise TypeError("System One response is not an object")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise TypeError("System One response is missing answers")
    parsed: dict[str, float] = {}
    for name, answer in answers.items():
        if not isinstance(answer, dict):
            raise TypeError(f"invalid System One answer for {name}")
        value = answer.get("noul")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"invalid System One answer for {name}")
        if not math.isfinite(float(value)) or not 0 <= value <= 1:
            message = f"invalid System One probability for {name}"
            raise ValueError(message)
        parsed[str(name)] = float(value)
    return parsed


class SystemOneClient:
    """Asks a Jev-shaped ``/v1/systemone`` endpoint over HTTP.

    Synchronous on purpose: the only callers are a background worker and the
    offline harness, and a synchronous client cannot accidentally be awaited
    from inside ``ContextEdit.apply``, which runs on every model call.
    """

    def __init__(
        self,
        *,
        endpoint: str = f"{DEFAULT_HOST}{SYSTEM_ONE_PATH}",
        model: str = DEFAULT_MODEL,
        api_key: str = "nova",
        timeout: float = 30.0,
        client: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._client = client

    def _session(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, float]:
        """POST one request. Raises on any transport or shape problem."""
        if not self.api_key:
            message = "Jev API key is missing; add it through /auth."
            raise ValueError(message)
        client = self._session()
        normalized = {
            name: question.model_dump() if hasattr(question, "model_dump") else question
            for name, question in questions.items()
        }
        response = client.post(
            self.endpoint,
            json={"model": self.model, "state": state, "questions": normalized},
            headers={"authorization": f"Bearer {self.api_key}"},
        )
        if response.status_code >= 400:
            message = f"System One request failed ({response.status_code})."
            raise ValueError(message)
        payload = response.json()
        if any(question.get("type") == "choice" for question in normalized.values()):
            return payload
        return parse_answers(payload)


def create_system_one_client(config: NovaConfig) -> DecisionClient:
    """Resolve credentials without sending TypeSafe keys to custom servers."""
    from novacode_cli.config.credentials import credential_value
    from novacode_cli.config.nova_config import NovaConfig

    endpoint = config.get_tool_verdict_endpoint()
    from novacode_cli.agents.openai_decisions import (
        OPENAI_DECISIONS_ENDPOINT,
        OpenAIDecisionsClient,
    )

    if endpoint == OPENAI_DECISIONS_ENDPOINT:
        return OpenAIDecisionsClient(
            model=config.get_tool_verdict_model(), api_key=credential_value("OPENAI_API_KEY")
        )
    if endpoint == NovaConfig.TOOL_VERDICT_JEV_ENDPOINT:
        api_key = credential_value("TYPESAFE_API_KEY")
    else:
        api_key = credential_value("SYSTEM_ONE_API_KEY") or "nova"
    return SystemOneClient(
        endpoint=endpoint, model=config.get_tool_verdict_model(), api_key=api_key
    )


def verdict_cache_path(agent_dir: Path | None, config: NovaConfig) -> Path | None:
    """Keep decisions from different endpoints/models in separate caches."""
    import hashlib

    if agent_dir is None:
        return None
    identity = _dumps([config.get_tool_verdict_endpoint(), config.get_tool_verdict_model()])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return agent_dir / "systemone" / digest / "tool_verdicts.json"


class FakeDecisionClient:
    """A client that answers from a fixed rule, for tests and dry runs."""

    def __init__(self, answer: Any = 0.5, calls: list[dict[str, Any]] | None = None) -> None:
        self._answer = answer
        self.calls = calls if calls is not None else []

    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, float]:
        self.calls.append({"state": state, "questions": questions})
        rule = self._answer
        return {name: float(rule(name)) if callable(rule) else float(rule) for name in questions}


# ── the verdict cache ───────────────────────────────────────────────────────
_CACHE_FILENAME = "tool_verdicts.json"

# Index files per window size, so removing the oldest cycle does not require
# rewriting the whole verdict store on every append. (Kept beside the other
# store constants; the pruning rule itself is expressed in cycles.)
_PAGE_SIZE = 32

# Total verdicts kept. An agent directory outlives one session, so this is
# bounded rather than unbounded: the oldest cycles are dropped when it is hit.
MAX_CACHED_VERDICTS = 2_000

# How long a scan of the store directory is reused before the read path looks
# again. Reads happen on the UI's event loop (``ContextEdit.apply`` runs on every
# model call), so the listing is memoised; this bounds how long a *different*
# process writing to the same agent directory stays invisible to this one.
# Missing a fresh cycle is safe -- an unscored result is kept, not cleared --
# it only means less pruning for a moment.
_LISTING_TTL_SECONDS = 2.0


def result_key(message: ToolMessage) -> str:
    """Identity of a result: its id *and* its bytes.

    A resumed session reuses ``tool_call_id``s -- one archive here had 444
    results under 122 distinct ids -- so the id alone is not an identity. Hashing
    the content in means a re-used id with different output is a different
    observation and gets scored again, while the same bytes are never scored
    twice.
    """
    import hashlib

    digest = hashlib.sha256()
    digest.update(str(message.tool_call_id).encode("utf-8"))
    digest.update(_format_content(message.content).encode("utf-8", "replace"))
    return digest.hexdigest()[:32]


@dataclass
class ToolVerdictCache:
    """Verdicts for a window of recent results, on disk beside the agent.

    Append-only, per cycle. A *cycle* is one scoring pass over the current
    window, so removing the newest cycle is an ``unlink`` and reading is newest
    first. Window size bounds a cycle's size, which bounds every read.

    Both halves of the read path are memoised (the directory scan and each
    parsed cycle) because ``ContextEdit.apply`` reads on **every model call**,
    synchronously, on the event loop the UI shares -- see
    :meth:`_cycle_entries` for what that cost before.

    Every filesystem error is swallowed, following the ``token_cache.json``
    pattern in :mod:`novacode_cli.token_utils`: a cache that cannot be written
    must slow the feature down, never break a turn.
    """

    path: Path | None = None
    cycle: int = 0
    window: int = MAX_VERDICT_WINDOW
    _memory: dict[str, float] = field(default_factory=dict)
    _loaded_cycle: int = -1
    # Cycle files parsed this session, and the directory scan the read path is
    # answering from. Both are for the same reason: reads run on the event loop
    # the UI shares, so neither may cost the filesystem more than once.
    _cycles: dict[int, dict[str, float]] = field(default_factory=dict)
    _listing: list[tuple[int, Path]] | None = None
    _listing_at: float = 0.0

    # ── paths ───────────────────────────────────────────────────────────
    @property
    def _dir(self) -> Path | None:
        return self.path.parent if self.path is not None else None

    def _cycle_path(self, cycle: int) -> Path | None:
        if self.path is None:
            return None
        return self.path.with_name(f"{self.path.stem}-{cycle}{self.path.suffix}")

    def cycle_files(self, *, fresh: bool = False) -> list[tuple[int, Path]]:
        """Existing cycle files, highest cycle first, highest within a page.

        Memoised for ``_LISTING_TTL_SECONDS``: a lookup that misses asks about
        every cycle, so a fresh scan per lookup was a scan per *result in the
        window*. ``fresh=True`` reads the directory now -- pruning deletes files
        and must decide from the directory, not from the scan it is about to
        replace.
        """
        if self.path is None:
            return []
        now = time.monotonic()
        if fresh or self._listing is None or now - self._listing_at >= _LISTING_TTL_SECONDS:
            self._listing = self._scan_cycle_files()
            self._listing_at = now
        return list(self._listing)

    def _scan_cycle_files(self) -> list[tuple[int, Path]]:
        """The directory, read once: existing cycles, highest first."""
        path = self.path
        if path is None:
            return []
        found: list[tuple[int, Path]] = []
        for candidate in path.parent.glob(f"{path.stem}-*{path.suffix}"):
            tail = candidate.stem.rsplit("-", 1)[-1]
            try:
                found.append((int(tail), candidate))
            except ValueError:
                continue
        found.sort(key=lambda pair: pair[0], reverse=True)
        return found

    # ── writing ─────────────────────────────────────────────────────────
    def begin(self) -> None:
        """Rotate in a new, empty cycle before a scoring pass."""
        self.cycle += 1
        self._memory = {}
        self._loaded_cycle = self.cycle

    def _prune(self) -> None:
        """Drop the oldest cycles once the store outgrows its budget.

        A cycle holds at most ``window`` verdicts, so the budget converts to a
        number of cycles. Compared as *cycle counts*: the previous version
        subtracted a cycle budget from a page-size multiple, which is a unit
        mismatch that silently disabled pruning entirely.
        """
        files = self.cycle_files(fresh=True)
        if not files:
            return
        max_cycles = max(1, MAX_CACHED_VERDICTS // max(1, self._cycle_size))
        if len(files) <= max_cycles:
            return
        removed = False
        for _, path in files[max_cycles:]:
            try:
                path.unlink(missing_ok=True)
                removed = True
            except OSError:
                pass
        if removed:
            # The memoised listing still names files this call just deleted.
            self._listing = None

    @property
    def _cycle_size(self) -> int:
        """Upper bound on verdicts one cycle can hold."""
        return max(1, self.window)

    def put(self, message: ToolMessage, noul: float) -> None:
        """Append a verdict to the current cycle."""
        key = result_key(message)
        self._memory[key] = float(noul)
        path = self._cycle_path(self.cycle)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({key: float(noul)}) + "\n")
            # These bytes may have created the current cycle's file, so the
            # memoised listing can no longer be trusted.
            self._listing = None
            self._prune()
        except OSError:
            logger.debug("Could not append a verdict to %s", path, exc_info=True)

    def flush(self) -> None:
        """Cycle accounting is per-append; present for symmetry."""

    # ── reading ─────────────────────────────────────────────────────────
    def warm(self) -> None:
        """Parse every cycle file now, so the first lookup finds them memoised.

        Lookups happen on the event loop the UI shares; the first one otherwise
        reads the whole store there (measured: 71 files, ~120 ms). Meant for a
        worker thread at agent-build time. Never raises.
        """
        try:
            for cycle, _path in self.cycle_files():
                self._cycle_entries(cycle)
        except Exception:  # noqa: BLE001 — a cold cache is only slower
            logger.debug("verdict cache warm-up failed", exc_info=True)

    def _cycle_entries(self, cycle: int) -> dict[str, float]:
        """The verdicts one cycle holds, read from disk at most once.

        ``ContextEdit.apply`` runs on every model call, on the event loop the UI
        shares, and a lookup that misses asks *every* cycle file. Measured on a
        real store (67 cycles, a 30-result window, every result a miss) against
        the pre-fix read path: 2,010 cycle parses and 6,030 filesystem calls per
        model call, ~1.0 s of the loop, 13 s on the first call. With the memo:
        one parse per cycle once, then nothing. A written cycle is append-only,
        so a parse of it cannot go out of date in a way that matters -- bytes
        appended by *another* process after this read are not seen, and a verdict
        that looks absent is read as "keep", which is the direction this whole
        edit fails in.
        """
        entries = self._cycles.get(cycle)
        if entries is not None:
            return entries
        entries = self._read_cycle(cycle)
        self._cycles[cycle] = entries
        return entries

    def _read_cycle(self, cycle: int) -> dict[str, float]:
        """One cycle file, parsed. An unreadable or absent file is empty."""
        path = self._cycle_path(cycle)
        if path is None or not path.exists():
            return {}
        entries: dict[str, float] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        for key, value in payload.items():
                            if isinstance(value, (int, float)):
                                entries[str(key)] = float(value)
        except OSError:
            return {}
        return entries

    def get(self, message: ToolMessage) -> float | None:
        """The cached probability for a result, or None when unscored.

        Newest cycle first, including the in-progress one, so a result scored a
        moment ago is found without touching the older pages.
        """
        key = result_key(message)
        if key in self._memory:
            return self._memory[key]
        for cycle, _ in self.cycle_files():
            found = self._cycle_entries(cycle).get(key)
            if found is not None:
                return found
        return None

    def stale(self, message: ToolMessage, *, keep_threshold: float) -> bool | None:
        """True when the cached verdict says clear it; None when unscored."""
        noul = self.get(message)
        if noul is None:
            return None
        return stale_from_noul(noul, keep_threshold=keep_threshold)

    def __len__(self) -> int:
        return len(self._memory)


# ── scoring, off the critical path ──────────────────────────────────────────
class VerdictScorer:
    """Keep a window of verdicts warm without ever blocking a turn.

    ``ContextEdit.apply`` runs synchronously on **every** model call, so it may
    not touch the network. This scorer is the other half: it is handed the
    message list when a turn is about to be sent, and it does its work on a
    background thread.

    Failures activate the normal age-based clearing edit. Scoring remains off
    the turn's critical path, and a successful retry restores verdict selection.
    """

    def __init__(
        self,
        client: DecisionClient,
        cache: ToolVerdictCache,
        *,
        keep: int = 5,
        window: int = MAX_VERDICT_WINDOW,
        max_state_tokens: int = DEFAULT_MAX_STATE_TOKENS,
        max_request_tokens: int = DEFAULT_MAX_REQUEST_TOKENS,
        min_interval_seconds: float = 2.0,
        fallback_after_seconds: float = 30.0,
    ) -> None:
        self.client = client
        self.cache = cache
        self.keep = keep
        self.window = window
        self.max_state_tokens = max_state_tokens
        self.max_request_tokens = max_request_tokens
        self.min_interval = min_interval_seconds
        self.fallback_after_seconds = fallback_after_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_started = 0.0
        self.last_error: str | None = None

    @property
    def fallback_required(self) -> bool:
        """Use normal clearing after a failed or overdue scoring pass."""
        import time

        with self._lock:
            return self.last_error is not None or (
                self._thread is not None
                and self._thread.is_alive()
                and time.monotonic() - self._last_started >= self.fallback_after_seconds
            )

    def maybe_score(self, messages: Sequence[AnyMessage]) -> bool:
        """Score the current window in the background. Returns whether it started."""
        import time

        now = time.monotonic()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if now - self._last_started < self.min_interval:
                return False
            candidates = collect_candidates(
                messages, preserve_recent_results=self.keep, window=self.window
            )
            unscored = [
                candidate
                for candidate in candidates
                if not candidate.pinned
                and self.cache.get(
                    ToolMessage(
                        content=candidate.content,
                        tool_call_id=candidate.call_id,
                        name=candidate.tool,
                    )
                )
                is None
            ]
            if not unscored:
                return False
            snapshot = list(messages)
            self._last_started = now
            self._thread = threading.Thread(
                target=self._score, args=(snapshot, unscored), daemon=True
            )
            self._thread.start()
            return True

    def _score(
        self,
        messages: Sequence[AnyMessage],
        candidates: Sequence[VerdictCandidate],
    ) -> None:
        """The worker: fit a bounded state, ask, append. Never raises."""
        try:
            recent = _slice_to_window(messages, self.window + self.keep, candidates)
            renumbered = collect_candidates(
                recent, preserve_recent_results=self.keep, window=self.window
            )
            if not renumbered:
                return
            fitted = build_verdict_state(
                recent,
                renumbered,
                max_state_tokens=self.max_state_tokens,
            )
            self.cache.begin()
            by_label = {candidate.label: candidate for candidate in renumbered}
            for batch in batch_candidates(
                renumbered, fitted.tokens, max_request_tokens=self.max_request_tokens
            ):
                questions: dict[str, dict[str, Any]] = {}
                for candidate in batch:
                    questions.update(questions_for(candidate))
                answers = self.client.ask(fitted.state, questions)
                expected = {
                    f"result_{candidate.label}" for candidate in batch if not candidate.pinned
                }
                if not expected.issubset(answers):
                    error_message = "Decision model returned incomplete tool-result verdicts"
                    raise ValueError(error_message)  # noqa: TRY301 - activate scoring fallback
                for name, noul in answers.items():
                    if not name.startswith("result_"):
                        continue
                    candidate = by_label.get(name[len("result_") :])
                    if candidate is None:
                        continue
                    self.cache.put(
                        ToolMessage(
                            content=candidate.content,
                            tool_call_id=candidate.call_id,
                            name=candidate.tool,
                        ),
                        noul,
                    )
            self.last_error = None
        except Exception as error:  # noqa: BLE001 - scoring is never worth a turn
            self.last_error = f"{type(error).__name__}: {error}"
            logger.debug("Verdict scoring failed", exc_info=True)


def _slice_to_window(
    messages: Sequence[AnyMessage],
    results: int,
    candidates: Sequence[VerdictCandidate],
) -> list[AnyMessage]:
    """The newest ``results`` tool results, whole messages only.

    Cut back to the assistant turn that made the oldest kept call, so every
    result in the slice still has the call the state describes it with.
    """
    indices = [index for index, message in enumerate(messages) if isinstance(message, ToolMessage)]
    if len(indices) <= results:
        return list(messages)
    cutoff = indices[-results]
    while cutoff > 0 and not (
        isinstance(messages[cutoff], AIMessage) and messages[cutoff].tool_calls
    ):
        cutoff -= 1
    return list(messages[cutoff:])


def build_verdict_middleware(scorer: VerdictScorer) -> Any:
    """Wrap a :class:`VerdictScorer` as an ``AgentMiddleware``.

    Scoring happens in ``awrap_model_call`` — the moment before a turn is sent —
    because that is the only place the full message list is available, and it is
    where the results the edit will read are already in place. The work itself is
    handed to the scorer's background thread and this returns immediately, so a
    slow or unreachable model never blocks a turn. The edit uses normal clearing
    once the scorer reports a failure or exceeds its time budget.

    Imported lazily so a disabled flag never pays for the middleware base class.
    """
    import threading

    from langchain.agents.middleware import AgentMiddleware

    threading.Thread(target=scorer.cache.warm, name="nova-verdict-warm", daemon=True).start()

    class _VerdictMiddleware(AgentMiddleware):  # type: ignore[misc]
        """Hands each turn's messages to the scorer, off the critical path."""

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            """Queue a scoring pass, then run the model call unchanged."""
            try:
                scorer.maybe_score(request.messages)
            except Exception:  # noqa: BLE001 - scoring never blocks or fails a turn
                logger.debug("Could not queue a scoring pass", exc_info=True)
            return await handler(request)

    return _VerdictMiddleware()
