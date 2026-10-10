"""Offline lifecycle retention and peak-memory measurements for the real TUI.

Run before and after changes in separate processes; no model calls are made.
"""

from __future__ import annotations

# Harness callbacks intentionally accept the application's dynamic constructor.
# ruff: noqa: INP001, ANN001, ANN002, ANN003, ANN202
import argparse
import asyncio
import gc
import json
import sys
import threading
import tracemalloc
import weakref
from pathlib import Path
from unittest.mock import patch

MAX_REPETITIONS = 10

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark_idle import working_set  # noqa: E402
from benchmark_tabs import measure  # noqa: E402


async def run(repetitions: int, *, baseline_observers: bool) -> tuple[dict, list]:
    """Repeatedly mount/unmount the same workload and count surviving apps."""
    from novacode_cli import events
    from novacode_cli.tui import gc_tuning
    from novacode_cli.tui.app import NovaApp

    references = []
    original = NovaApp.__init__

    def record(app, *args, **kwargs):
        original(app, *args, **kwargs)
        references.append(weakref.ref(app))

    tracemalloc.start()
    samples = []
    release = NovaApp._release_output_observers

    def baseline_gc():
        gc.collect()
        gc.freeze()
        gc.set_threshold(*gc_tuning.THRESHOLDS)
        return True

    with (
        patch.object(NovaApp, "__init__", record),
        patch.object(
            NovaApp,
            "_release_output_observers",
            (lambda _app: None) if baseline_observers else release,
        ),
        patch.object(gc_tuning, "tune", baseline_gc if baseline_observers else gc_tuning.tune),
    ):
        for _ in range(repetitions):
            samples.append(await measure(1, 100, 1))
            await asyncio.sleep(0)
            gc.collect()
    await asyncio.sleep(0.2)
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = working_set()
    return {
        "repetitions": repetitions,
        "baseline_observers": baseline_observers,
        "tool_output_callbacks": len(events._output_callbacks),
        "traced_retained_bytes": retained,
        "traced_peak_bytes": peak,
        "working_set_bytes": rss,
        "samples": samples,
    }, references


def main() -> None:
    """Write a content-free JSON artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-observers", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= MAX_REPETITIONS:
        parser.error("repetitions must be 1-10")
    result, references = asyncio.run(
        run(args.repetitions, baseline_observers=args.baseline_observers)
    )
    gc.collect()
    result["apps_surviving_gc_after_loop_close"] = sum(ref() is not None for ref in references)
    result["frozen_objects"] = gc.get_freeze_count()
    result["background_threads"] = [thread.name for thread in threading.enumerate()]
    referrer_types = {}
    for reference in references:
        app = reference()
        if app is not None:
            for referrer in gc.get_referrers(app):
                name = type(referrer).__name__
                referrer_types[name] = referrer_types.get(name, 0) + 1
    result["survivor_referrer_types"] = referrer_types
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
