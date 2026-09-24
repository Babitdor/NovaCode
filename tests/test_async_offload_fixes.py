"""Prove the C1/C2 fixes actually move the blocking work off the event loop.

Runs the real async entry points and asserts the loop stayed responsive while
the (deliberately slowed) sync work ran. A ticker task counts loop iterations; a
stalled loop cannot tick.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

#: How long the stand-in blocking work pretends to take.
_BLOCK_SECS = 0.4

#: Ticker interval, and the minimum ticks a responsive loop completes in
#: ``_BLOCK_SECS``. 10 is ~1/4 of the theoretical 40, leaving ample headroom
#: while still being impossible if the loop were blocked.
_TICK = 0.01
_MIN_TICKS = 10


async def _count_loop_ticks(work: Callable[[], Awaitable[Any]]) -> int:
    """Run ``work`` and count how many loop iterations fit alongside it.

    Args:
        work: Zero-arg callable returning the awaitable doing the (possibly
            blocking) work.

    Returns:
        Number of ticker iterations that ran while ``work`` was in flight.
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(_TICK)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await work()
    finally:
        task.cancel()
    return ticks


async def test_relevant_memories_async_does_not_block_the_loop(tmp_path: Path) -> None:
    """C1: the corpus load must run off-loop, so a slow read can't stall it."""
    from novacode_cli.memory.agent_memory import AgentMemoryMiddleware

    mw = AgentMemoryMiddleware.__new__(AgentMemoryMiddleware)
    mw.agent_dir = tmp_path
    mw._corpus_cache = None
    mw._corpus_sig = None
    mw._retrieval_cache = None

    def slow_load() -> dict:
        time.sleep(_BLOCK_SECS)  # stand-in for the glob+read of a large corpus
        return {}

    mw._load_memory_corpus = slow_load  # type: ignore[method-assign]
    mw._latest_user_text = lambda _r: "some query about memory"  # type: ignore[method-assign]

    ticks = await _count_loop_ticks(lambda: mw._relevant_memories_async(MagicMock()))

    # If the sleep had run on the loop, the ticker could not have fired.
    assert ticks >= _MIN_TICKS, f"loop was blocked: only {ticks} ticks during the load"


async def test_apply_review_content_does_not_block_the_loop(tmp_path: Path) -> None:
    """C2: persisting a review must run off-loop."""
    from novacode_cli.hermes.review import ReviewRunner

    rev = ReviewRunner.__new__(ReviewRunner)
    rev._enabled = True
    rev._agent_dir = tmp_path

    def slow_persist(_parsed: dict[str, object], _content: str) -> None:
        time.sleep(_BLOCK_SECS)

    async def fake_count() -> int:
        return 1

    async def aput(*_a: Any, **_k: Any) -> None:
        return None

    rev._persist_review = slow_persist  # type: ignore[method-assign]
    rev._get_review_count = fake_count  # type: ignore[method-assign]
    rev._store = MagicMock()
    rev._store.aput = aput

    async def work() -> None:
        # Later steps need more stubs than are worth building here; the timing
        # assertion is the contract under test, not the outcome.
        with suppress(Exception):
            await rev._apply_review_content("<user_model></user_model>")

    ticks = await _count_loop_ticks(work)

    assert ticks >= _MIN_TICKS, f"loop was blocked: only {ticks} ticks during the persist"


def test_append_refinement_events_batches_one_write(tmp_path: Path) -> None:
    """C2: N events must cost ONE read + ONE write, not N of each."""
    from novacode_cli.hermes import refinement_log

    reads = {"n": 0}
    writes = {"n": 0}
    real_read, real_write = refinement_log._read_events, refinement_log._write_events

    def counting_read(path: Path) -> list[dict[str, object]]:
        reads["n"] += 1
        return real_read(path)

    def counting_write(path: Path, events: list[dict[str, object]]) -> None:
        writes["n"] += 1
        real_write(path, events)

    refinement_log._read_events = counting_read  # type: ignore[assignment]
    refinement_log._write_events = counting_write  # type: ignore[assignment]
    try:
        ids = refinement_log.append_refinement_events(
            tmp_path,
            [{"domain": "memory", "action": "record_lesson", "target": f"t{i}"} for i in range(20)],
        )
    finally:
        refinement_log._read_events = real_read  # type: ignore[assignment]
        refinement_log._write_events = real_write  # type: ignore[assignment]

    assert len(ids) == 20
    assert reads["n"] == 1, f"expected 1 read for 20 events, got {reads['n']}"
    assert writes["n"] == 1, f"expected 1 write for 20 events, got {writes['n']}"

    # And the single-event wrapper still works.
    one = refinement_log.append_refinement_event(
        tmp_path, domain="memory", action="record_lesson", target="solo"
    )
    assert one
    assert len(refinement_log.read_refinement_events(tmp_path, limit=100)) == 21
