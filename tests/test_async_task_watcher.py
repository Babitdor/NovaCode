"""AsyncTaskWatcher: watch only running async-subagent tasks, dedup, and notify
with the right level when one finishes (closing the fire-and-forget gap)."""

from __future__ import annotations

import asyncio

from novacode_cli.remote.async_task_watcher import AsyncTaskWatcher


async def test_watches_only_running_and_dedups():
    scheduled: list[str] = []

    w = AsyncTaskWatcher(notify=lambda *a: None)

    async def fake_poll(task):
        scheduled.append(task["task_id"])

    w._poll = fake_poll  # avoid real SDK/network

    tasks = {
        "a": {"task_id": "a", "status": "running", "agent_name": "x", "thread_id": "a", "run_id": "r"},
        "b": {"task_id": "b", "status": "success", "agent_name": "x"},  # not running → skip
    }
    w.sync_from_state(tasks)
    w.sync_from_state(tasks)  # same tasks again → 'a' must not be re-watched
    await asyncio.sleep(0.02)

    assert scheduled == ["a"]  # only the running one, exactly once


async def test_ignores_empty_and_malformed():
    w = AsyncTaskWatcher(notify=lambda *a: None)
    w._poll = lambda task: asyncio.sleep(0)  # never records
    w.sync_from_state(None)
    w.sync_from_state({"x": "not-a-dict"})
    await asyncio.sleep(0.01)
    assert set(w._watched) == set()


async def test_running_tasks_lists_watched_with_runtime():
    w = AsyncTaskWatcher(notify=lambda *a: None)

    async def slow(task):  # keep it "running"
        await asyncio.sleep(5)

    w._poll = slow
    w.sync_from_state(
        {"a": {"task_id": "a", "status": "running", "agent_name": "code-review-agent",
               "thread_id": "a", "run_id": "r"}}
    )
    rt = w.running_tasks()
    assert len(rt) == 1
    assert rt[0]["agent_name"] == "code-review-agent"
    assert rt[0]["runtime"] >= 0
    for t in list(w._tasks):
        t.cancel()


async def test_poll_removes_from_running_on_exit():
    w = AsyncTaskWatcher(notify=lambda *a: None)
    w._running["x"] = {"agent_name": "unknown-agent", "started": 0.0}
    # Unknown agent → no spec → _poll returns immediately; its finally pops _running.
    await w._poll({"task_id": "x", "agent_name": "unknown-agent", "thread_id": "x", "run_id": "r"})
    assert "x" not in w._running
    assert w.running_tasks() == []


def test_emit_maps_status_to_level_and_message():
    got: list[tuple] = []
    w = AsyncTaskWatcher(notify=lambda level, title, msg: got.append((level, title, msg)))

    w._emit("code-review-agent", "abcdef123456", "success")
    w._emit("documentation-update-agent", "zzzzzzzz1111", "error")

    assert got[0][0] == "success"
    assert "code-review-agent" in got[0][2] and "abcdef12" in got[0][2]

    assert got[1][0] == "warning"
    assert "error" in got[1][1].lower()
