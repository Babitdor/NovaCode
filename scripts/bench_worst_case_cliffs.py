"""Measure the worst-case cliffs that were fixed but not previously benched.

1. OutputBody.replace_lines head-eviction: the old scan tried every split
   point with slice compares (O(n^2) when nothing matches); the new one only
   tries candidates where old_ids[k] == new_ids[0].
2. subagent_tasks._ROW_CACHE: the old cache cleared wholesale at >512 entries;
   the new one trims the oldest 128.
3. The async preview poller: the old @work(exclusive=True) cancelled the first
   preview's poller when a second preview opened. Measured as poll counts:
   the frozen preview stops polling the moment the second opens.

Each section measures the old behavior (reconstructed verbatim) against the
new one. Run: uv run python scripts/bench_worst_case_cliffs.py

ruff: noqa: T201
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
import time

sys.path.insert(0, ".")

import textual._work_decorator as work_mod
from rich.text import Text

from novacode_cli.core import subagent_tasks as st
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.output_body import OutputBody
from novacode_cli.ui.ui_elements import TokenTracker
from novacode_cli.ui_events import SubagentTask

results: dict = {}


# ── 1. Head-eviction scan ───────────────────────────────────────────────────


def old_split(old_ids: list[int], new_ids: list[int], new_len: int) -> tuple[int, int]:
    """The pre-fix eviction search: first matching k, every k tried."""
    for k in range(1, len(old_ids) + 1):
        tail = old_ids[k:]
        if new_ids[: len(tail)] == tail:
            return k, new_len - len(tail)
    return len(old_ids), 0


def new_split(old_ids: list[int], new_ids: list[int], new_len: int) -> tuple[int, int]:
    """The post-fix candidate scan: only k where old_ids[k] == new_ids[0]."""
    trim = len(old_ids)
    if new_ids:
        pos = 1
        while True:
            try:
                k = old_ids.index(new_ids[0], pos)
            except ValueError:
                break
            pos = k + 1
            tail = old_ids[k:]
            if new_ids[: len(tail)] == tail:
                return k, new_len - len(tail)
    return trim, 0


def bench_eviction() -> None:
    sections = {}
    for size in (100, 400, 1000):
        old_ids = [id(Text()) for _ in range(size)]
        # Rewrite-style stream: every line brand new, nothing matches.
        new_ids = [id(Text()) for _ in range(size)]

        # Correctness: both must agree on a real eviction case.
        evicted = old_ids.copy()
        evicted[0] = -1
        assert old_split(evicted, old_ids[1:] + [-2], size) == new_split(
            evicted, old_ids[1:] + [-2], size
        ), "old and new disagree on an eviction case"

        runs = {}
        for label, fn in (("old", old_split), ("new", new_split)):
            times = []
            for _ in range(5):
                start = time.perf_counter()
                for _ in range(20):
                    fn(old_ids, new_ids, size)
                times.append((time.perf_counter() - start) / 20 * 1000)
            runs[label] = round(statistics.median(times), 2)
        sections[f"n={size}"] = {
            "old_ms_per_flush": runs["old"],
            "new_ms_per_flush": runs["new"],
            "ratio": round(runs["old"] / max(runs["new"], 1e-9), 1),
        }
    results["eviction_scan"] = sections


# ── 2. Row cache ────────────────────────────────────────────────────────────


def _finished_task(task_id: str) -> SubagentTask:
    return SubagentTask(
        task_id=task_id,
        status="done",
        phase_kind="eval",
        subagent_type="researcher",
        label=f"research: docling-{task_id}",
        duration_ms=38_300,
    )


def old_task_row(task, *, width, now, selected=False):
    """The pre-fix cache policy, verbatim: wholesale clear past 512."""
    if task.status in {"done", "failed", "stopped"}:
        if st._ROW_CACHE_WIDTH != width:
            st._ROW_CACHE.clear()
            st._ROW_CACHE_WIDTH = width
        key = (
            task.task_id,
            width,
            selected,
            task.status,
            task.label,
            task.error,
            task.compacting,
        )
        cached = st._ROW_CACHE.get(key)
        if cached is None:
            if len(st._ROW_CACHE) > 512:
                st._ROW_CACHE.clear()
            cached = st._build_task_row(task, width=width, now=now, selected=selected)
            st._ROW_CACHE[key] = cached
        return cached
    return st._build_task_row(task, width=width, now=now, selected=selected)


def bench_row_cache() -> None:
    builds = {"count": 0}
    original_build = st._build_task_row

    def counting_build(*args, **kwargs):
        builds["count"] += 1
        return original_build(*args, **kwargs)

    st._build_task_row = counting_build
    try:
        sections = {}
        for label, render in (("old_clear", old_task_row), ("new_trim", st.task_row)):
            st._ROW_CACHE.clear()
            st._ROW_CACHE_WIDTH = 140
            builds["count"] = 0
            start = time.perf_counter()
            window: list = []
            seq = 0
            for tick in range(40):  # 40 s of dock life at 1 Hz
                for _ in range(20):
                    seq += 1
                    window.append(_finished_task(f"t{seq}"))
                window = window[-200:]
                now = 1000.0 + tick
                for task in window:
                    render(task, width=140, now=now)
            elapsed = time.perf_counter() - start
            sections[label] = {
                "row_builds": builds["count"],
                "tick_wall_ms": round(elapsed / 40 * 1000, 2),
            }
        results["row_cache"] = {
            "old_clear": sections["old_clear"],
            "new_trim": sections["new_trim"],
            "build_ratio": round(
                sections["old_clear"]["row_builds"] / max(sections["new_trim"]["row_builds"], 1), 1
            ),
        }
    finally:
        st._build_task_row = original_build
        st._ROW_CACHE.clear()
        st._ROW_CACHE_WIDTH = -1


# ── 3. Preview poller ───────────────────────────────────────────────────────


class _FakeThreads:
    counter: dict = {}

    async def get_state(self, thread_id):
        self.counter[thread_id] = self.counter.get(thread_id, 0) + 1
        return {
            "values": {"messages": [{"type": "ai", "content": f"tick {self.counter[thread_id]}"}]}
        }


class _FakeClient:
    def __init__(self, url=None, headers=None):
        self.threads = _FakeThreads()


def _make_app(app_cls) -> NovaApp:
    class _FakeAgent:
        async def aget_state(self, _config):
            return type("State", (), {"values": {"messages": []}})()

    class _SS:
        thread_id = "t1"
        session_id = "s1"
        auto_approve = False
        plan_mode_enabled = False
        todos: list = []
        steering_instructions: list = []

    app = app_cls(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )
    app._home_style = "minimal"
    return app


async def _bench_poller_for(app_cls) -> dict:
    _FakeThreads.counter = {}
    app = _make_app(app_cls)
    async with app.run_test(size=(140, 44)) as pilot:
        import novacode_cli.agents.default_subagents.async_subagents as async_sub

        async_sub.retrieve_async_subagents = lambda: [
            {"name": "researcher", "url": "http://agent-server", "headers": {}}
        ]
        import langgraph_sdk

        langgraph_sdk.get_client = lambda url=None, headers=None: _FakeClient()

        def _row(task_id, thread):
            return SubagentTask(
                task_id=task_id,
                status="running",
                phase_kind="async",
                subagent_type="researcher",
                description=thread,
                started_at=100.0,
            )

        app._subagent_rows["async:1"] = _row("async:1", "thread-1")
        app._subagent_rows["async:2"] = _row("async:2", "thread-2")
        app._open_subagent_preview("async:1")
        await pilot.pause(2.5)
        app._open_subagent_preview("async:2")
        await pilot.pause(4.5)
        return {
            "preview1_polls": _FakeThreads.counter.get("thread-1", 0),
            "preview2_polls": _FakeThreads.counter.get("thread-2", 0),
        }


def bench_preview_poller() -> dict:
    """Poll counts per preview over a 4.5 s window after opening the second."""
    raw = NovaApp._follow_async_preview.__wrapped__

    class OldApp(NovaApp):
        @work_mod.work(exclusive=True, group="subagent-preview")
        async def _follow_async_preview(self, task_id, screen):
            await raw(self, task_id, screen)

    # Textual resolves a relative CSS_PATH against the class's module; the
    # subclass lives in scripts/, so pin the stylesheet to the app's own.
    import novacode_cli.tui.app as tui_app
    from pathlib import Path

    app_dir = Path(tui_app.__file__).parent
    if isinstance(NovaApp.CSS_PATH, str):
        OldApp.CSS_PATH = str(app_dir / NovaApp.CSS_PATH)
    else:
        OldApp.CSS_PATH = [str(app_dir / p) for p in NovaApp.CSS_PATH]

    old = asyncio.run(_bench_poller_for(OldApp))
    new = asyncio.run(_bench_poller_for(NovaApp))
    return {"old_exclusive": old, "new_concurrent": new}


def main() -> None:
    bench_eviction()
    bench_row_cache()
    results["preview_poller"] = bench_preview_poller()

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
