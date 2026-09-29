"""One-off: how many real sessions can this approach actually run on?

Prints, per archived transcript, how many tool results it has and how big the
conversation skeleton becomes once every shrink stage has run. The two numbers
together decide viability: a session needs *both* enough results to be worth
pruning and a skeleton that fits the model's context.
"""

from __future__ import annotations

import statistics
import sys

sys.path.insert(0, ".")

from novacode_cli.agents.tool_verdicts import (  # noqa: E402
    build_verdict_state,
    collect_candidates,
)
from scripts.measure_tool_verdicts import load_transcript, transcript_paths  # noqa: E402

rows: list[tuple[int, int | None]] = []
for path in transcript_paths():
    messages = load_transcript(path)
    candidates = collect_candidates(messages, preserve_recent_results=5)
    needed: int | None = None
    try:
        state = build_verdict_state(messages, candidates, max_state_tokens=10**9)
        needed = state.tokens
    except ValueError:
        needed = None
    rows.append((len(candidates), needed))

rows.sort(key=lambda row: -row[0])
print(f"total archived transcripts: {len(rows)}")
fits = [needed for _, needed in rows if needed is not None]
usable = [row for row in rows if row[1] is not None and row[0] > 5]
print(f"skeletons that fit at any size : {len(fits)}/{len(rows)}")
print(f"median skeleton when it fits    : {statistics.median(fits):.0f} tokens")
print(f"VIABLE (>5 results AND fits)    : {len(usable)}")
print()
print("results | skeleton tokens  (15 largest by result count)")
for count, needed in rows[:15]:
    shown = needed if needed is not None else "too large even fully shrunk"
    print(f"{count:>7} | {shown}")
