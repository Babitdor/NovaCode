"""One-off: run the real Tev1 against every usable transcript and sweep the threshold.

The measurement the plan's Phase 5 asks for: at matched reclaimed characters, does
the verdict path regret fewer clears than the keep-last-N heuristic? The first
endpoint run showed every verdict landing just above 0.5, so the threshold sweep
is what finds whether *any* setting makes Tev1 prune usefully.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from novacode_cli.agents.tool_verdicts import (  # noqa: E402
    MAX_VERDICT_WINDOW,
    SystemOneClient,
    collect_candidates,
)
from scripts.measure_tool_verdicts import (  # noqa: E402
    _needed_tokens,
    char_tokens,
    collect_verdicts,
    load_transcript,
    measure_regret,
    run_heuristic,
    run_verdicts,
    transcript_paths,
)

ENDPOINT = "http://127.0.0.1:11434/v1/systemone"
MODEL = "tev1:0.8b"
TRIGGER = 1024
KEEP = 5
THRESHOLDS = (0.5, 0.6, 0.65, 0.7, 0.8)

workdir = pathlib.Path(tempfile.mkdtemp())
totals: dict[float, list] = {threshold: [] for threshold in THRESHOLDS}
heuristic_totals: list = []
sessions = 0

header = (
    f"{'transcript':<28}{'res':>5}{'jdg':>6}  {'noul range':<12}"
    + "".join(f"{'t' + str(t):>16}" for t in THRESHOLDS)
)
print(header)
print("-" * len(header))

for path in transcript_paths():
    messages = load_transcript(path)
    total = sum(1 for message in messages if hasattr(message, "tool_call_id"))
    if total <= KEEP:
        continue

    candidates = collect_candidates(
        messages, preserve_recent_results=KEEP, window=MAX_VERDICT_WINDOW
    )
    try:
        cache, _ = collect_verdicts(
            messages,
            candidates,
            SystemOneClient(endpoint=ENDPOINT, model=MODEL, timeout=120),
            max_state_tokens=1400,
            max_request_tokens=1800,
            window=MAX_VERDICT_WINDOW,
            keep=KEEP,
        )
    except ValueError as error:
        print(
            f"{path.name[:27]:<28}{total:>5}{'TOO BIG':>6}  "
            f"needs ~{_needed_tokens(str(error))} tokens"
        )
        continue

    values = sorted(cache._memory.values())
    if not values:
        continue

    offload = workdir / path.stem
    offload.mkdir(parents=True, exist_ok=True)
    _, heuristic_cleared = run_heuristic(
        messages, trigger=TRIGGER, keep=KEEP, offload=offload, count_tokens=char_tokens
    )
    heuristic = measure_regret(messages, heuristic_cleared)
    heuristic_totals.append(heuristic)

    line = f"{path.name[:27]:<28}{total:>5}{len(cache):>6}  {min(values):.2f}-{max(values):.2f}  "
    for threshold in THRESHOLDS:
        _, cleared = run_verdicts(
            messages,
            cache=cache,
            trigger=TRIGGER,
            keep=KEEP,
            offload=offload,
            keep_threshold=threshold,
            count_tokens=char_tokens,
        )
        regret = measure_regret(messages, cleared)
        totals[threshold].append(regret)
        line += f"  c{regret.cleared:>3} rg{regret.regretted:>3} {regret.per_1k:>4.2f}"
    print(line)
    sessions += 1

print()
print(f"sessions run against a real model: {sessions}")
print()

h_cleared = sum(r.cleared for r in heuristic_totals)
h_regretted = sum(r.regretted for r in heuristic_totals)
h_chars = sum(r.reclaimed_chars for r in heuristic_totals)
print(
    f"HEURISTIC  : cleared {h_cleared}, regretted {h_regretted}, "
    f"{h_chars} chars, {h_regretted * 1000 / max(1, h_chars):.2f} regretted/1k"
)
for threshold in THRESHOLDS:
    rows = totals[threshold]
    cleared = sum(r.cleared for r in rows)
    regretted = sum(r.regretted for r in rows)
    chars = sum(r.reclaimed_chars for r in rows)
    per_k = regretted * 1000 / max(1, chars)
    print(
        f"THRESHOLD {threshold:<4}: cleared {cleared:>4}, regretted {regretted:>4}, "
        f"{chars:>7} chars, {per_k:>5.2f} regretted/1k"
    )
