"""Fire-and-forget tasks that cannot be garbage-collected mid-flight.

The event loop keeps only a weak reference to a task, so one started with a
bare ``asyncio.create_task(...)`` and never stored can be collected before it
finishes (the asyncio docs call this out). ``spawn`` holds the reference until
the task is done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_live: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """``asyncio.create_task`` that keeps the task alive until it completes."""
    task = asyncio.create_task(coro)
    _live.add(task)
    task.add_done_callback(_live.discard)
    return task


if __name__ == "__main__":
    # ponytail: self-check — the task is held while running and released after.
    async def _demo() -> None:
        done = asyncio.Event()

        async def work() -> None:
            await asyncio.sleep(0)
            done.set()

        spawn(work())
        assert len(_live) == 1
        await done.wait()
        await asyncio.sleep(0)
        assert not _live

    asyncio.run(_demo())
    print("ok")
