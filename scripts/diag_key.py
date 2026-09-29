"""Diagnostic: what does the scorer store, before and after the injected defect?"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from novacode_cli.agents.tool_verdicts import (  # noqa: E402
    FakeDecisionClient,
    ToolVerdictCache,
    VerdictScorer,
    collect_candidates,
    result_key,
)


def history(turns: int = 20) -> list:
    messages: list = []
    for n in range(1, turns + 1):
        messages.append(HumanMessage(content=f"ask {n}", id=f"h{n}"))
        messages.append(
            AIMessage(
                content="",
                id=f"a{n}",
                tool_calls=[
                    {"id": f"c{n}", "name": "read_file", "args": {"file_path": f"/f{n}"}}
                ],
            )
        )
        messages.append(
            ToolMessage("x" * 100, tool_call_id=f"c{n}", name="read_file", id=f"t{n}")
        )
    return messages


messages = history()
results = [m for m in messages if isinstance(m, ToolMessage)]
candidates = collect_candidates(messages, preserve_recent_results=5, window=12)
print(f"results={len(results)} candidates={len(candidates)}")
print(f"candidate[0].content[:20] = {candidates[0].content[:20]!r}")
print(f"candidate[0].content_chars = {candidates[0].content_chars}")

cache = ToolVerdictCache(path=None)
scorer = VerdictScorer(FakeDecisionClient(0.1), cache, min_interval_seconds=0.0)
scorer.maybe_score(messages)
import time

for _ in range(200):
    thread = scorer._thread
    if thread is None or not thread.is_alive():
        break
    time.sleep(0.01)

print(f"last_error={scorer.last_error!r}")
print(f"stored={len(cache._memory)}")
stored = set(cache._memory)
expected = {result_key(m) for m in results}
print(f"matched={len(stored & expected)} of {len(stored)}")
# Show why: does candidate.content actually equal the message content?
sample = results[-6]
print(f"message content     = {sample.content[:20]!r}")
print(f"key(message)        = {result_key(sample)}")
print(f"any stored key == that? {result_key(sample) in stored}")
