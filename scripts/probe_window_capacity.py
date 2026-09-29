"""One-off: how many *recent* results fit the model's context?

The whole-session skeleton does not fit (see probe_fit_capacity.py). This asks
the follow-up question the bounded design needs answered: if only the newest N
results are described, what is the largest N that fits, and what would that
window have been able to prune?
"""

from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.messages import AnyMessage

sys.path.insert(0, ".")

from novacode_cli.agents.tool_verdicts import (  # noqa: E402
    build_verdict_state,
    collect_candidates,
)
from scripts.measure_tool_verdicts import load_transcript, transcript_paths  # noqa: E402


def slice_recent(messages: list[AnyMessage], results: int) -> list[AnyMessage]:
    """The messages from the Nth-most-recent tool result onwards.

    Keeps whole messages: a result whose call is cut away would lose the
    description the state needs to say what the call was.
    """
    from langchain_core.messages import ToolMessage

    indices = [
        index for index, message in enumerate(messages) if isinstance(message, ToolMessage)
    ]
    if len(indices) <= results:
        return list(messages)
    cutoff = indices[-results]
    # walk back to the assistant turn that made the call
    while cutoff > 0 and not _is_ai_with_calls(messages[cutoff]):
        cutoff -= 1
    return list(messages[cutoff:])


def _is_ai_with_calls(message: AnyMessage) -> bool:
    from langchain_core.messages import AIMessage

    return isinstance(message, AIMessage) and bool(message.tool_calls)


def largest_fitting_window(messages: list[AnyMessage], budget: int) -> tuple[int, int]:
    """The biggest recent-result window whose skeleton fits *budget* tokens."""
    best = (0, 0)
    for window in (80, 60, 48, 40, 32, 24, 16, 12, 8, 6, 4, 2):
        sliced = slice_recent(messages, window)
        candidates = collect_candidates(sliced, preserve_recent_results=5)
        try:
            state = build_verdict_state(sliced, candidates, max_state_tokens=budget)
        except ValueError:
            continue
        return (window, state.tokens)
    return best


budgets = (1_000, 1_500, 1_800)
rows: list[tuple[str, int, dict[int, tuple[int, int]]]] = []
for path in transcript_paths():
    messages = load_transcript(path)
    total = sum(1 for m in messages if hasattr(m, "tool_call_id"))
    if total <= 5:
        continue
    per_budget = {}
    for budget in budgets:
        window, tokens = largest_fitting_window(messages, budget)
        per_budget[budget] = (window, tokens)
    rows.append((path.name, total, per_budget))

print(f"transcripts with more than 5 results: {len(rows)}")
print()
header = f"{'transcript':<30}{'results':>8}" + "".join(f"{b:>18}" for b in budgets)
print(header)
print("-" * len(header))
for name, total, per_budget in rows:
    cells = ""
    for budget in budgets:
        window, tokens = per_budget[budget]
        cells += f"{window:>10}r/{tokens:>6}t"
    print(f"{name[:29]:<30}{total:>8}{cells}")

print()
for budget in budgets:
    windows = [per_budget[budget][0] for _, _, per_budget in rows]
    if windows:
        print(
            f"budget {budget:>5}: window that fits — min {min(windows)}, "
            f"max {max(windows)}, median {sorted(windows)[len(windows) // 2]}"
        )
