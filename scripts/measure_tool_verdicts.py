"""Measure verdict-driven pruning against the keep-last-N heuristic.

The question this answers is not "does it clear tool results" -- both schemes
do. It is **at the same tokens reclaimed, which one regrets fewer clears**. A
clear is *regretted* when the agent comes back for the same thing later: the
same tool called again with the same normalized arguments, after the result was
already cleared. That is the agent re-deriving what the context used to hold,
and it is the cost the heuristic pays without ever knowing.

Both selectors run through the **shipped** edit classes against **real**
transcripts, so the comparison measures the product rather than a model of it.
``--self-check`` pins that: the simulator's heuristic path must produce exactly
what ``_tool_result_clearing`` produces on the same messages.

Usage::

    uv run python scripts/measure_tool_verdicts.py --self-check
    uv run python scripts/measure_tool_verdicts.py --limit 10
    uv run python scripts/measure_tool_verdicts.py --limit 10 --endpoint \\
        http://localhost:11434/v1/systemone --model tev1

Without ``--endpoint`` the verdicts come from ``--stale-probability``, which is
a placeholder: it shows the harness works and the shape of the trade-off, not
what Tev1 would decide. The real measurement needs an endpoint.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from langchain_core.messages import AnyMessage, ToolMessage

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # runnable as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from novacode_cli.agents.core_agent import _context_edit_trigger  # noqa: E402
from novacode_cli.agents.tool_offload import (  # noqa: E402
    OffloadingToolUsesEdit,
    VerdictToolUsesEdit,
    cleared_dir,
)
from novacode_cli.agents.tool_verdicts import (  # noqa: E402
    DEFAULT_KEEP_THRESHOLD,
    MAX_VERDICT_WINDOW,
    DecisionClient,
    FakeDecisionClient,
    SystemOneClient,
    ToolVerdictCache,
    batch_candidates,
    build_verdict_state,
    collect_candidates,
    questions_for,
)

#: Sessions live here; ``pre-compact-*.jsonl`` is the raw transcript.
SESSIONS_DIR = Path.home() / ".nova" / "sessions"


# ── loading ─────────────────────────────────────────────────────────────────
def _content(payload: dict[str, Any]) -> Any:
    """Message content from a dump.

    ``model_dump()`` stores content as plain text when it is text and as the
    already-serialized block dicts when it is not, so an LC-serialized envelope
    (``{"lc": 1, ...}``) has to be unwrapped and everything else passed through.
    """
    value = payload.get("content")
    if isinstance(value, dict) and "lc" in value:
        return value.get("kwargs", {}).get("content", "")
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    parts.append(f"[Called tool: {block.get('name', 'unknown_tool')}]")
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    if isinstance(value, str):
        return value
    return value if value is not None else ""


def load_transcript(path: Path) -> list[AnyMessage]:
    """Rebuild messages from an archived pre-compaction transcript.

    The archive is ``json.dumps(message.model_dump())``, one per line. It is
    rebuilt by type rather than through ``langchain_core.load.load``, which
    returns plain dicts for this shape (the content blocks are already
    serialized, so there is nothing in the envelope to tell LangChain what to
    construct). A line whose type is not a message is dropped: the transcript
    only needs the conversation, and guessing at unrelated dumps would put
    objects into the message list that no reducer expects.
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    messages: list[AnyMessage] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            content = _content(payload)
            try:
                if kind == "human":
                    messages.append(HumanMessage(content=content, id=payload.get("id")))
                elif kind == "ai":
                    messages.append(
                        AIMessage(
                            content=content,
                            tool_calls=[
                                {
                                    "id": call.get("id") or f"call-{index}",
                                    "name": call.get("name") or "tool",
                                    "args": call.get("args") or {},
                                }
                                for index, call in enumerate(payload.get("tool_calls") or [])
                            ],
                            id=payload.get("id"),
                        )
                    )
                elif kind == "tool":
                    messages.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=payload.get("tool_call_id") or "",
                            name=payload.get("name"),
                            id=payload.get("id"),
                        )
                    )
                elif kind == "system":
                    messages.append(SystemMessage(content=content, id=payload.get("id")))
            except Exception:  # noqa: BLE001 - one malformed message is not fatal
                continue
    return messages


def transcript_paths(limit: int | None = None, *, min_results: int = 0) -> list[Path]:
    """Archived transcripts, most tool results first.

    Ordered by *result count*, not file size: what these selectors act on is the
    tool results, and a large transcript of long prose exercises nothing. A
    size-ordered walk picks the trivial sessions and reports zeros.
    """
    found = [Path(p) for p in glob.glob(str(SESSIONS_DIR / "*" / "pre-compact-*.jsonl"))]
    scored: list[tuple[int, Path]] = []
    for path in found:
        try:
            results = sum(
                1
                for message in load_transcript(path)
                if isinstance(message, ToolMessage)
            )
        except OSError:
            continue
        if results < min_results:
            continue
        scored.append((results, path))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1])))
    paths = [path for _, path in scored]
    return paths[:limit] if limit else paths


def slice_recent(messages: Sequence[AnyMessage], results: int) -> list[AnyMessage]:
    """The newest ``results`` tool results, whole messages only.

    Cut back to the assistant turn that made the oldest kept call, so every
    result in the slice still carries the call the state describes it with.
    """
    from langchain_core.messages import AIMessage

    indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage)
    ]
    if len(indices) <= results:
        return list(messages)
    cutoff = indices[-results]
    while cutoff > 0 and not (
        isinstance(messages[cutoff], AIMessage) and messages[cutoff].tool_calls
    ):
        cutoff -= 1
    return list(messages[cutoff:])


def call_key(message: ToolMessage) -> tuple[str, str]:
    """Identity of an observation: the tool and its normalized arguments.

    Deliberately not the ``tool_call_id``: an agent that re-derives something
    rarely reuses the id, and the whole point is to catch the second call.
    """
    return (message.name or "tool", _normalize(message.content))


def _normalize(content: Any) -> str:
    text = str(content)
    return text[:80].strip()


# ── regret ──────────────────────────────────────────────────────────────────
@dataclass
class Regret:
    """How often the agent had to go back for something that was cleared."""

    cleared: int = 0
    regretted: int = 0
    reclaimed_chars: int = 0
    cleared_keys: list[tuple[str, str]] = field(default_factory=list)

    @property
    def per_1k(self) -> float:
        """Regretted clears per 1,000 characters reclaimed."""
        if self.reclaimed_chars <= 0:
            return 0.0
        return self.regretted * 1_000 / self.reclaimed_chars


def measure_regret(
    messages: Sequence[AnyMessage],
    cleared_ids: set[str],
) -> Regret:
    """Score one selector's outcome against what the session did next."""
    result = Regret()
    if not cleared_ids:
        return result

    # Where each cleared result sat, and what the agent asked for afterwards.
    positions: dict[str, int] = {}
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage) and message.tool_call_id in cleared_ids:
            positions[message.tool_call_id] = index
            result.cleared += 1
            result.reclaimed_chars += len(str(message.content))
            result.cleared_keys.append(call_key(message))

    for call_id, index in positions.items():
        # A later call for the same observation is the agent re-deriving it.
        original = next(
            message
            for message in messages
            if isinstance(message, ToolMessage) and message.tool_call_id == call_id
        )
        key = call_key(original)
        for later in messages[index + 1 :]:
            if isinstance(later, ToolMessage) and call_key(later) == key:
                result.regretted += 1
                break
    return result


# ── the two selectors ───────────────────────────────────────────────────────
def run_heuristic(
    messages: Sequence[AnyMessage],
    *,
    trigger: int,
    keep: int,
    offload: Path,
    count_tokens: Callable[[Sequence[AnyMessage]], int],
) -> tuple[list[AnyMessage], set[str]]:
    """Today's selection, through the shipped edit class."""
    working = list(messages)
    before = {
        message.tool_call_id: str(message.content)
        for message in working
        if isinstance(message, ToolMessage)
    }
    OffloadingToolUsesEdit(
        trigger=trigger,
        keep=keep,
        clear_tool_inputs=False,
        exclude_tools=["think"],
        placeholder="[cleared]",
        offload_dir=offload,
    ).apply(working, count_tokens=count_tokens)

    cleared = {
        message.tool_call_id
        for message in working
        if isinstance(message, ToolMessage) and str(message.content) != before.get(message.tool_call_id)
    }
    return working, cleared


def collect_verdicts(
    messages: Sequence[AnyMessage],
    candidates: Sequence[Any],
    client: DecisionClient,
    *,
    max_state_tokens: int,
    max_request_tokens: int,
    window: int | None = MAX_VERDICT_WINDOW,
    keep: int = 5,
) -> tuple[ToolVerdictCache, str]:
    """Score the newest ``window`` results and return a filled cache.

    Whole-message slicing, because the state that describes a result needs the
    call it came from: scoring only the newest results is what makes the state
    fit at all (see :data:`MAX_VERDICT_WINDOW`).
    """
    cache = ToolVerdictCache(path=None)
    if not candidates:
        return cache, ""

    recent = slice_recent(messages, (window or 0) + keep) if window else list(messages)
    renumbered = collect_candidates(
        recent, preserve_recent_results=keep, window=window
    )
    if not renumbered:
        return cache, ""

    fitted = build_verdict_state(
        recent, renumbered, max_state_tokens=max_state_tokens
    )
    by_label = {candidate.label: candidate for candidate in renumbered}
    cache.begin()
    for batch in batch_candidates(
        renumbered, fitted.tokens, max_request_tokens=max_request_tokens
    ):
        questions: dict[str, dict[str, Any]] = {}
        for candidate in batch:
            questions.update(questions_for(candidate))
        answers = client.ask(fitted.state, questions)
        for name, noul in answers.items():
            if not name.startswith("result_"):
                continue
            candidate = by_label.get(name[len("result_") :])
            if candidate is not None:
                cache.put(
                    ToolMessage(
                        content=candidate.content,
                        tool_call_id=candidate.call_id,
                        name=candidate.tool,
                    ),
                    noul,
                )
    return cache, fitted.stage


def run_verdicts(
    messages: Sequence[AnyMessage],
    *,
    cache: ToolVerdictCache,
    trigger: int,
    keep: int,
    offload: Path,
    keep_threshold: float,
    count_tokens: Callable[[Sequence[AnyMessage]], int],
) -> tuple[list[AnyMessage], set[str]]:
    """The verdict-driven selection, through the shipped edit class."""
    working = list(messages)
    before = {
        message.tool_call_id: str(message.content)
        for message in working
        if isinstance(message, ToolMessage)
    }
    VerdictToolUsesEdit(
        trigger=trigger,
        keep=keep,
        clear_tool_inputs=False,
        exclude_tools=["think"],
        placeholder="[cleared]",
        offload_dir=offload,
        verdicts=cache,
        keep_threshold=keep_threshold,
    ).apply(working, count_tokens=count_tokens)

    cleared = {
        message.tool_call_id
        for message in working
        if isinstance(message, ToolMessage) and str(message.content) != before.get(message.tool_call_id)
    }
    return working, cleared


# ── a fixed-price token counter, so the trigger is comparable ───────────────
def char_tokens(messages: Sequence[AnyMessage]) -> int:
    """Deterministic stand-in for the model's counter: 4 characters a token.

    The real counter is the model's tokenizer, which the edit receives at call
    time. For a comparison that has to hold the trigger fixed across both
    selectors, any consistent counter will do.
    """
    return sum(len(str(getattr(message, "content", ""))) for message in messages) // 4


# ── self-check ──────────────────────────────────────────────────────────────
def self_check(offload: Path) -> int:
    """Prove the harness measures the product, not a re-implementation.

    The heuristic path must produce byte-identical output to the production
    reducer, and the verdict path with a keep-everything cache must produce the
    heuristic's output too -- because "the model wants everything kept" is
    exactly the case where verdicts must clear nothing.

    Runs on the largest transcript that *fits* the model's context, not simply
    the largest: the ones that do not fit are a separate, reported outcome, and
    self-checking on one of them would fail this gate for the wrong reason.

    Note ``_tool_result_clearing(0, ...)`` is **not** "no trigger": a
    non-positive window resolves to the 60,000 fallback. This check passed
    ``0`` at first and compared an edit that cleared nothing against one that
    cleared everything, so both paths are constructed with an explicit trigger.
    """
    from novacode_cli.agents.core_agent import _tool_result_clearing

    chosen: Path | None = None
    for path in transcript_paths():
        messages = load_transcript(path)
        candidates = collect_candidates(
            messages, preserve_recent_results=5, window=MAX_VERDICT_WINDOW
        )
        if len(candidates) < 3:
            continue
        recent = slice_recent(messages, MAX_VERDICT_WINDOW + 5)
        renumbered = collect_candidates(
            recent, preserve_recent_results=5, window=MAX_VERDICT_WINDOW
        )
        try:
            build_verdict_state(recent, renumbered, max_state_tokens=1_000)
        except ValueError:
            continue
        chosen = path
        break

    if chosen is None:
        print("self-check: no transcript both fits the context and has results")
        return 1

    messages = load_transcript(chosen)
    print(f"self-check on {chosen.name}: {len(messages)} messages")

    # The reducer resolves its trigger from a *context window*, not a token
    # count, so the comparison picks the window whose resolved trigger lands
    # below this transcript and therefore actually fires on it.
    transcript_tokens = char_tokens(messages)
    window = 0
    for candidate_window in (4_096, 8_192, 16_384, 32_768, 65_536, 131_072):
        resolved = _context_edit_trigger(candidate_window)
        if 0 < resolved < transcript_tokens:
            window = candidate_window
            break
    if not window:
        print(
            f"FAIL: no context window resolves to a trigger under "
            f"{transcript_tokens} tokens, so the reducer cannot be exercised"
        )
        return 1
    trigger = _context_edit_trigger(window)

    # Compare by position, not by a dict keyed on `tool_call_id`: a resumed
    # session reuses ids, so the dict would silently collapse duplicates and
    # report agreement it never checked.
    original = [str(message.content) for message in messages if isinstance(message, ToolMessage)]

    production = list(messages)
    _tool_result_clearing(window, offload).apply(production, count_tokens=char_tokens)
    produced = [
        str(message.content)
        for message in production
        if isinstance(message, ToolMessage)
    ]
    production_cleared = sum(
        1 for before, after in zip(original, produced) if before != after
    )

    harness_history, harness_cleared = run_heuristic(
        messages, trigger=trigger, keep=5, offload=offload, count_tokens=char_tokens
    )
    harnessed = [
        str(message.content)
        for message in harness_history
        if isinstance(message, ToolMessage)
    ]
    if produced != harnessed:
        print(
            "FAIL: the heuristic path does not reproduce _tool_result_clearing "
            f"({production_cleared} cleared by production, "
            f"{len(harness_cleared)} by the harness)"
        )
        return 1
    if not production_cleared:
        print(
            f"FAIL: the production reducer cleared nothing at window {window} "
            f"(trigger {trigger}, transcript {transcript_tokens} tokens)"
        )
        return 1
    print(f"  window {window} -> trigger {trigger}, transcript {transcript_tokens} tokens")

    # keep-everything verdicts must clear nothing
    keep_all = FakeDecisionClient(1.0)
    try:
        cache, _ = collect_verdicts(
            messages,
            collect_candidates(messages, preserve_recent_results=5),
            keep_all,
            max_state_tokens=1_000,
            max_request_tokens=1_800,
            window=MAX_VERDICT_WINDOW,
            keep=5,
        )
    except ValueError as error:
        print(f"FAIL: even the fake verdicts could not be built: {error}")
        return 1
    _, verdict_cleared = run_verdicts(
        messages,
        cache=cache,
        trigger=trigger,
        keep=5,
        offload=offload,
        keep_threshold=DEFAULT_KEEP_THRESHOLD,
        count_tokens=char_tokens,
    )
    if verdict_cleared:
        print(
            f"FAIL: keep-everything verdicts cleared {len(verdict_cleared)} results"
        )
        return 1
    print(
        f"PASS: production reducer reproduced ({production_cleared} cleared); "
        "keep-everything verdicts cleared 0"
    )
    return 0


# ── the comparison ──────────────────────────────────────────────────────────
@dataclass
class Row:
    name: str
    messages: int
    results: int
    judged: int
    heuristic: Regret
    verdicts: Regret
    state_stage: str
    requests: int


def compare(
    paths: Iterable[Path],
    *,
    client_factory: Callable[[], DecisionClient],
    keep: int,
    keep_threshold: float,
    max_state_tokens: int,
    max_request_tokens: int,
    window: int | None,
    workdir: Path,
) -> tuple[list[Row], list[tuple[str, int]]]:
    """Score every transcript, returning the rows and the ones that would not fit.

    With a bounded ``window`` nothing should fail to fit: that is the whole
    point of the bound. Anything that still cannot be fitted is reported rather
    than skipped, because it is a session where no verdict can be asked.
    """
    rows: list[Row] = []
    too_large: list[tuple[str, int]] = []
    for path in paths:
        messages = load_transcript(path)
        total = sum(1 for message in messages if isinstance(message, ToolMessage))
        if total <= keep:
            continue
        offload = workdir / path.stem
        offload.mkdir(parents=True, exist_ok=True)

        # A trigger below every transcript, so both selectors run on everything:
        # the comparison is between *selections*, not between thresholds.
        trigger = 0
        _, heuristic_cleared = run_heuristic(
            messages, trigger=trigger, keep=keep, offload=offload, count_tokens=char_tokens
        )

        try:
            cache, stage = collect_verdicts(
                messages,
                collect_candidates(messages, preserve_recent_results=keep, window=window),
                client_factory(),
                max_state_tokens=max_state_tokens,
                max_request_tokens=max_request_tokens,
                window=window,
                keep=keep,
            )
        except ValueError as error:
            needed = _needed_tokens(str(error))
            too_large.append((path.name, needed))
            continue

        _, verdict_cleared = run_verdicts(
            messages,
            cache=cache,
            trigger=trigger,
            keep=keep,
            offload=offload,
            keep_threshold=keep_threshold,
            count_tokens=char_tokens,
        )

        rows.append(
            Row(
                name=path.name,
                messages=len(messages),
                results=total,
                judged=len(cache),
                heuristic=measure_regret(messages, heuristic_cleared),
                verdicts=measure_regret(messages, verdict_cleared),
                state_stage=stage,
                requests=1,
            )
        )
    return rows, too_large


def _needed_tokens(text: str) -> int:
    if "~" not in text:
        return 0
    try:
        return int(text.split("~")[1].split()[0])
    except (IndexError, ValueError):
        return 0


def report(rows: Sequence[Row], too_large: Sequence[tuple[str, int]] = ()) -> None:
    if not rows:
        print("no transcripts measured")
        return
    header = (
        f"{'transcript':<30}{'res':>6}{'jdg':>5}{'heur':>6}{'hRg':>5}{'vr':>5}{'vRg':>5}"
        f"{'h/kch':>8}{'v/kch':>8}{'stage':>18}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.name[:29]:<30}{row.results:>6}{row.judged:>5}{row.heuristic.cleared:>6}"
            f"{row.heuristic.regretted:>5}{row.verdicts.cleared:>5}"
            f"{row.verdicts.regretted:>5}{row.heuristic.per_1k:>8.2f}"
            f"{row.verdicts.per_1k:>8.2f}{row.state_stage:>18}"
        )

    h_cleared = sum(row.heuristic.cleared for row in rows)
    v_cleared = sum(row.verdicts.cleared for row in rows)
    h_regretted = sum(row.heuristic.regretted for row in rows)
    v_regretted = sum(row.verdicts.regretted for row in rows)
    h_chars = sum(row.heuristic.reclaimed_chars for row in rows)
    v_chars = sum(row.verdicts.reclaimed_chars for row in rows)

    print()
    print(f"transcripts      : {len(rows)}")
    print(f"results in them  : {sum(row.results for row in rows)}")
    print(f"results judged   : {sum(row.judged for row in rows)} "
          f"(the bounded window only)")
    print(f"heuristic        : cleared {h_cleared}, regretted {h_regretted}, "
          f"{h_chars} chars, {h_regretted * 1000 / max(1, h_chars):.2f} regretted/1k chars")
    print(f"verdicts         : cleared {v_cleared}, regretted {v_regretted}, "
          f"{v_chars} chars, {v_regretted * 1000 / max(1, v_chars):.2f} regretted/1k chars")
    print()
    print("A lower regretted/1k at comparable reclaimed characters is the win.")

    if too_large:
        print()
        print(
            f"COULD NOT RUN ({len(too_large)}): the conversation skeleton exceeds "
            "the model's context even after every shrink stage, so no verdict can "
            "be asked and these sessions keep the heuristic."
        )
        for name, needed in too_large:
            print(f"  {name[:44]:<46}~{needed} tokens needed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="how many transcripts")
    parser.add_argument("--keep", type=int, default=5, help="recent results kept")
    parser.add_argument(
        "--window",
        type=int,
        default=MAX_VERDICT_WINDOW,
        help="how many recent results a verdict may be asked about",
    )
    parser.add_argument(
        "--no-window", action="store_true", help="score the whole session (will not fit)"
    )
    parser.add_argument("--keep-threshold", type=float, default=DEFAULT_KEEP_THRESHOLD)
    parser.add_argument("--max-state-tokens", type=int, default=1_000)
    parser.add_argument("--max-request-tokens", type=int, default=1_800)
    parser.add_argument("--endpoint", help="System One endpoint; omit for fake verdicts")
    parser.add_argument("--model", default="tev1")
    parser.add_argument(
        "--stale-probability",
        type=float,
        default=0.2,
        help="fake verdicts: probability that a result is still needed",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="pin the harness against the production reducer and exit",
    )
    parser.add_argument("--workdir", default=None, help="scratch dir for offloads")
    args = parser.parse_args(argv)

    import tempfile

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="verdicts-"))

    if args.self_check:
        return self_check(workdir)

    paths = transcript_paths(limit=args.limit)
    if not paths:
        print(f"no transcripts under {SESSIONS_DIR}")
        return 1

    if args.endpoint:
        def factory() -> DecisionClient:
            return SystemOneClient(endpoint=args.endpoint, model=args.model)
        source = f"endpoint {args.endpoint} ({args.model})"
    else:
        def factory() -> DecisionClient:
            return FakeDecisionClient(args.stale_probability)
        source = (
            f"FAKE verdicts (p(keep)={args.stale_probability}) -- "
            "not a measurement of any model"
        )

    print(f"verdict source : {source}")
    print(f"transcripts    : {len(paths)}")
    print()
    rows, too_large = compare(
        paths,
        client_factory=factory,
        keep=args.keep,
        keep_threshold=args.keep_threshold,
        max_state_tokens=args.max_state_tokens,
        max_request_tokens=args.max_request_tokens,
        window=None if args.no_window else args.window,
        workdir=workdir,
    )
    report(rows, too_large)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
