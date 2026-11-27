"""Measure the Textual UI loop's responsiveness while background work storms.

Run: uv run python scripts/bench_ui_responsiveness.py
Output: JSON with frame-tick jitter, UI-thread stalls, and input latency —
measured in-process against the real NovaApp under three load phases
(shell-job churn, tool-output streaming, subagent dock fan-out).

ruff: noqa: T201
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
import time
from collections import deque

sys.path.insert(0, ".")

from rich.text import Text

from novacode_cli.core import subagent_tasks as st
from novacode_cli.shell.jobs import JobRegistry
from novacode_cli.tui.app import NovaApp
from novacode_cli.ui.ui_elements import TokenTracker
from novacode_cli.ui_events import SubagentTask

TICK_MS = 16  # one frame at 60 Hz: any gap above this is a stall candidate


class _FakeAgent:
    async def aget_state(self, _config: object) -> object:
        return type("State", (), {"values": {"messages": []}})()


class _SS:
    thread_id = "t1"
    session_id = "s1"
    auto_approve = False
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


def _app() -> NovaApp:
    app = NovaApp(
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


class FrameProbe:
    """A 16 ms timer that records tick-to-tick gaps on the UI loop."""

    def __init__(self) -> None:
        self.gaps_ms: list[float] = []
        self._last = time.perf_counter()
        self._timer = None

    def start(self, app: NovaApp) -> None:
        self._timer = app.set_interval(TICK_MS / 1000, self._tick)

    def _tick(self) -> None:
        now = time.perf_counter()
        self.gaps_ms.append((now - self._last) * 1000)
        self._last = now

    def summary(self) -> dict:
        gaps = sorted(self.gaps_ms)
        if not gaps:
            return {}

        def pct(p):
            return round(gaps[min(len(gaps) - 1, int(len(gaps) * p))], 1)

        return {
            "ticks": len(gaps),
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "p99_ms": pct(0.99),
            "max_ms": round(gaps[-1], 1),
            "stalls_over_100ms": sum(1 for g in gaps if g > 100),
            "stalls_over_250ms": sum(1 for g in gaps if g > 250),
        }


class RenderMeter:
    """Accumulates time Textual's compositor spends rendering updates."""

    def __init__(self) -> None:
        self.total_ms = 0.0
        self.calls = 0
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        from textual.screen import Screen

        original = Screen._compositor_refresh

        def timed(screen_self):
            start = time.perf_counter()
            try:
                return original(screen_self)
            finally:
                self.total_ms += (time.perf_counter() - start) * 1000
                self.calls += 1

        Screen._compositor_refresh = timed
        self._installed = True

    def reset(self) -> None:
        self.total_ms = 0.0
        self.calls = 0

    def take(self) -> dict:
        out = {"render_ms": round(self.total_ms, 1), "render_calls": self.calls}
        self.reset()
        return out


WORDS = [
    "compiled",
    "src/mod.ts",
    "OK",
    "warning:",
    "unused",
    "import",
    "rustc",
    "error[E0382]",
    "borrow",
    "checker",
    "npm",
    "vite",
    "built",
    "in",
    "142ms",
]


def _job_storm(registry: JobRegistry, seconds: float, rng: random.Random) -> int:
    """Spawn jobs, stream log chunks through them, complete them."""
    events = 0
    deadline = time.perf_counter() + seconds
    jobs = []
    while time.perf_counter() < deadline:
        while len(jobs) < 6:
            jobs.append(registry.add("uv run pytest -q " + "x" * 40, tool_name="tui-shell"))
            events += 1
        for job in jobs:
            registry.append_log(job.id, " ".join(rng.choices(WORDS, k=30)) + "\n")
            events += 1
        if rng.random() < 0.3 and jobs:
            job = jobs.pop(0)
            registry.complete(job.id, 0)
            events += 1
        time.sleep(0.02)
    for job in jobs:
        registry.complete(job.id, 0)
    return events


def _tool_output_storm(app: NovaApp, seconds: float, rng: random.Random) -> int:
    """Feed tool output the way the middleware producer does."""
    events = 0
    deadline = time.perf_counter() + seconds
    calls = [f"call_{n}" for n in range(4)]
    while time.perf_counter() < deadline:
        for call_id in calls:
            chunk = " ".join(rng.choices(WORDS, k=80)) + "\n"
            app._on_tool_output(call_id, chunk)
            events += 1
        time.sleep(0.03)
    return events


def _dock_storm(app: NovaApp, seconds: float, rng: random.Random) -> int:
    """Fan out subagent rows: starts, previews, completions."""
    events = 0
    deadline = time.perf_counter() + seconds
    n = 0
    live: list[SubagentTask] = []
    while time.perf_counter() < deadline:
        n += 1
        task = SubagentTask(
            task_id=f"ptc_task_{n:06d}",
            status="running",
            phase_kind="eval",
            phase_id=f"call_eval_{n % 4}",
            subagent_type="researcher",
            label=f"research: docling-{n}",
            started_at=time.time(),
        )
        live.append(task)
        # The real producer posts stream events to the UI loop; the ingest
        # path paints via set_timer, which is UI-loop-only.
        app._post_to_ui(app._ingest_subagent_task, task)
        events += 1
        if len(live) > 12:
            done = live.pop(0)
            done.status = "done"
            done.duration_ms = 38_300
            app._post_to_ui(app._ingest_subagent_task, done)
            events += 1
        time.sleep(0.04)
    return events


def _home_animation(app: NovaApp, seconds: float) -> int:
    """The idle home screen: matrix rain animating."""
    app._show_home_banner()
    time.sleep(seconds)
    return 0


def _heavy_transcript(app: NovaApp, seconds: float, rng: random.Random) -> int:
    """Transcript over the 200-widget cap while streaming output into logs."""
    from novacode_cli.tui.widgets import OutputLog

    transcript = app._transcript()
    logs = []
    for n in range(3):
        log = OutputLog(max_lines=1000, max_chars=200_000)
        log.classes = "logline"
        app.call_from_thread(transcript.mount, log)
        logs.append(log)
    # Push past the widget cap: the prune path engages on every refresh.
    for n in range(230):
        app._post_to_ui(app._log, Text(f"history line {n}: " + "context " * 12), transcript=True)
    events = 0
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        for log in logs:
            app._post_to_ui(
                log.write,
                Text(" ".join(rng.choices(WORDS, k=60))),
            )
            events += 1
        time.sleep(0.03)
    for log in logs:
        app._post_to_ui(log.remove)
    return events


async def _measure_input_latency(app: NovaApp, pilot, count: int) -> list[float]:
    """Time from posting a key event to the app having processed it."""
    latencies = []
    for _ in range(count):
        start = time.perf_counter()
        await pilot.press("a")
        # pilot.press returns after the key has been processed.
        latencies.append((time.perf_counter() - start) * 1000)
        await asyncio.sleep(0.05)
    return latencies


async def main() -> None:
    rng = random.Random(2026)
    app = _app()
    results: dict = {"phases": {}}
    async with app.run_test(size=(140, 44)) as pilot:
        probe = FrameProbe()
        probe.start(app)
        registry = JobRegistry()
        app._task_registry = registry

        phases = [
            ("idle_baseline", None),
            ("home_matrix_rain", None),
            ("job_registry_storm", lambda: _job_storm(registry, 6.0, rng)),
            ("tool_output_stream", lambda: _tool_output_stream(app, 6.0, rng)),
            ("subagent_dock_fanout", lambda: _dock_storm(app, 6.0, rng)),
            ("heavy_transcript_over_cap", lambda: _heavy_transcript(app, 6.0, rng)),
            ("tasks_panel_during_storm", None),
            ("input_latency_check", None),
        ]
        render_meter = RenderMeter()
        render_meter.install()
        input_latencies: list[float] = []
        for name, load in phases:
            mark = len(probe.gaps_ms)
            render_meter.reset()
            events = 0
            if name == "input_latency_check":
                input_latencies = await _measure_input_latency(app, pilot, 20)
            elif name == "home_matrix_rain":
                app._show_home_banner()
                await asyncio.sleep(6.0)
            elif name == "tasks_panel_during_storm":
                from novacode_cli.tui.screens import BackgroundTasksScreen

                screen = BackgroundTasksScreen(registry=registry)
                app.push_screen(screen)
                await pilot.pause()
                thread_result: dict = {}
                load_task = asyncio.create_task(asyncio.to_thread(_job_storm, registry, 5.0, rng))
                await asyncio.sleep(5.2)
                load_task.cancel()
                app.pop_screen()
            elif load is not None:
                thread_result: dict = {}

                async def run_load(fn=load):
                    await asyncio.to_thread(fn)

                # The producer-side storms run on worker threads like the real
                # producers; give the UI loop the same 6 s to cope.
                task = asyncio.create_task(run_load(load))
                await asyncio.sleep(6.2)
                if not task.done():
                    task.cancel()
                events = 0
            await asyncio.sleep(0.3)
            gaps = probe.gaps_ms[mark:]
            phase_result = {"ticks": 0, **render_meter.take()}
            if gaps:
                gaps_sorted = sorted(gaps)
                phase_result.update(
                    {
                        "ticks": len(gaps_sorted),
                        "p50_ms": round(gaps_sorted[len(gaps_sorted) // 2], 1),
                        "p95_ms": round(gaps_sorted[int(len(gaps_sorted) * 0.95)], 1),
                        "max_ms": round(gaps_sorted[-1], 1),
                        "stalls_over_100ms": sum(1 for g in gaps if g > 100),
                        "stalls_over_250ms": sum(1 for g in gaps if g > 250),
                    }
                )
            results["phases"][name] = phase_result

        results["input_latency_ms"] = {
            "p50": round(statistics.median(input_latencies), 1) if input_latencies else None,
            "max": round(max(input_latencies), 1) if input_latencies else None,
        }
        results["overall"] = FrameProbe.summary(probe)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
