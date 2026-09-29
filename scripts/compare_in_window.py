"""The apples-to-apples comparison the sweep was missing.

The sweep compared the verdict path over its 12-result window against the
heuristic over the whole session -- 49 cleared against 859 -- which measures the
window, not the selection. This caps both to the *same* results: the newest
window the model can be asked about. Only then does regret/1k answer whether the
verdicts pick better than "everything but the last 5".
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from langchain_core.messages import ToolMessage  # noqa: E402

from novacode_cli.agents.tool_verdicts import (  # noqa: E402
    MAX_VERDICT_WINDOW,
    SystemOneClient,
    collect_candidates,
)
from scripts.measure_tool_verdicts import (  # noqa: E402
    char_tokens,
    collect_verdicts,
    load_transcript,
    measure_regret,
    run_verdicts,
    slice_recent,
    transcript_paths,
)

ENDPOINT = "http://127.0.0.1:11434/v1/systemone"
MODEL = "tev1:4b"
KEEP = 5
WINDOW = MAX_VERDICT_WINDOW
TRIGGER = 1024

workdir = pathlib.Path(tempfile.mkdtemp())

print(f"model: {MODEL} | window: {WINDOW} results | keep: {KEEP}")
print()
header = (
    f"{'transcript':<28}{'res':>5}{'win':>5}"
    f"{'heuristic (in window)':>24}{'verdicts (in window)':>24}"
)
print(header)
print("-" * len(header))

totals = {"heur": [0, 0, 0], "verd": [0, 0, 0]}
sessions = 0

for path in transcript_paths():
    messages = load_transcript(path)
    total = sum(1 for m in messages if isinstance(m, ToolMessage))
    if total <= KEEP + 1:
        continue

    # Both selectors see exactly the same slice: the newest window+keep results.
    sliced = slice_recent(messages, WINDOW + KEEP)
    window_results = sum(1 for m in sliced if isinstance(m, ToolMessage))
    if window_results <= KEEP + 1:
        continue

    candidates = collect_candidates(
        sliced, preserve_recent_results=KEEP, window=WINDOW
    )
    try:
        cache, _ = collect_verdicts(
            sliced,
            candidates,
            SystemOneClient(endpoint=ENDPOINT, model=MODEL, timeout=300),
            max_state_tokens=1400,
            max_request_tokens=1800,
            window=WINDOW,
            keep=KEEP,
        )
    except Exception as error:  # noqa: BLE001 - a probe reports failures
        print(f"{path.name[:27]:<28} model failed: {type(error).__name__}")
        continue
    if not cache._memory:
        continue

    offload = workdir / path.stem
    offload.mkdir(parents=True, exist_ok=True)

    # The heuristic, restricted to the same window: clear all but the last KEEP.
    heuristic_messages = list(sliced)
    from novacode_cli.agents.tool_offload import OffloadingToolUsesEdit, cleared_dir

    before = {
        m.tool_call_id: str(m.content)
        for m in heuristic_messages
        if isinstance(m, ToolMessage)
    }
    OffloadingToolUsesEdit(
        trigger=0,
        keep=KEEP,
        exclude_tools=["think"],
        placeholder="[cleared]",
        offload_dir=cleared_dir(offload),
    ).apply(heuristic_messages, count_tokens=char_tokens)
    heuristic_cleared = {
        m.tool_call_id
        for m in heuristic_messages
        if isinstance(m, ToolMessage) and str(m.content) != before.get(m.tool_call_id)
    }
    heuristic = measure_regret(sliced, heuristic_cleared)

    _, verdict_cleared = run_verdicts(
        sliced,
        cache=cache,
        trigger=0,
        keep=KEEP,
        offload=offload,
        keep_threshold=0.5,
        count_tokens=char_tokens,
    )
    verdict = measure_regret(sliced, verdict_cleared)

    print(
        f"{path.name[:27]:<28}{total:>5}{window_results:>5}"
        f"   c{heuristic.cleared:>3} rg{heuristic.regretted:>2} {heuristic.per_1k:>5.2f}/k"
        f"   c{verdict.cleared:>3} rg{verdict.regretted:>2} {verdict.per_1k:>5.2f}/k"
    )
    for key, regret in (("heur", heuristic), ("verd", verdict)):
        totals[key][0] += regret.cleared
        totals[key][1] += regret.regretted
        totals[key][2] += regret.reclaimed_chars
    sessions += 1

print()
print(f"sessions: {sessions}")
for label, key in (("HEURISTIC", "heur"), ("VERDICTS ", "verd")):
    cleared, regretted, chars = totals[key]
    print(
        f"{label}: cleared {cleared:>4}, regretted {regretted:>4}, "
        f"{chars:>8} chars reclaimed, "
        f"{regretted * 1000 / max(1, chars):>5.2f} regretted/1k"
    )
print()
print("Within the same window, a lower regretted/1k means the verdicts picked better.")
