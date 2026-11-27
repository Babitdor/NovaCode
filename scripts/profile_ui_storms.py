"""Profile the UI loop while the responsiveness storms run (top functions)."""

import asyncio
import cProfile
import io
import pstats
import random
import sys

sys.path.insert(0, ".")

from scripts.bench_ui_responsiveness import (  # noqa: E402
    FrameProbe,
    _app,
    _dock_storm,
    _job_storm,
    _tool_output_storm,
)
from novacode_cli.shell.jobs import JobRegistry  # noqa: E402

import random as _random


async def main() -> None:
    rng = _random.Random(2026)
    app = _app()
    profiler = cProfile.Profile()
    async with app.run_test(size=(140, 44)) as pilot:
        probe = FrameProbe()
        probe.start(app)
        registry = JobRegistry()
        app._task_registry = registry

        profiler.enable()
        await asyncio.to_thread(_job_storm, registry, 4.0, rng)
        await asyncio.sleep(0.5)
        await asyncio.to_thread(_tool_output_storm, app, 4.0, rng)
        await asyncio.sleep(0.5)
        await asyncio.to_thread(_dock_storm, app, 4.0, rng)
        await asyncio.sleep(0.5)
        profiler.disable()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("cumulative").print_stats(28)
    text = stream.getvalue()
    # Keep the table, drop the boilerplate header lines
    lines = text.splitlines()
    keep = [
        line
        for line in lines
        if line.strip()
        and (
            "ncalls" in line
            or "/" in line
            or "novacode" in line
            or "textual" in line
            or "rich" in line
        )
    ]
    print("\n".join(keep[:40]))


if __name__ == "__main__":
    asyncio.run(main())
