"""Measure isolated headless idle Nova sessions without API or voice models.

Run: python scripts/benchmark_idle.py --sessions 2 --seconds 5
Each session is a separate process; the parent prints JSON measurements.
"""

from __future__ import annotations

# Standalone JSON-reporting measurement command.
# ruff: noqa: INP001, T201
import argparse
import asyncio
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MAX_SESSIONS = 4
sys.path.insert(0, str(ROOT))


class Agent:
    """Empty model boundary; the real TUI still mounts and runs its timers."""

    async def aget_state(self, _config: object) -> object:
        """Expose an empty checkpoint without invoking a model."""
        return type("State", (), {"values": {"messages": []}})()


class Session:
    """Minimal inactive session for the benchmark."""

    thread_id = "benchmark"
    auto_approve = True
    plan_mode_enabled = False
    todos = None
    steering_instructions = ()


def working_set() -> int | None:
    """Read current resident bytes on Windows/Linux, or report unavailable."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, object]]] = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "PeakWorkingSetSize",
                        "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage",
                        "PagefileUsage",
                        "PeakPagefileUsage",
                    )
                ],
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize
    path = Path("/proc/self/statm")
    if path.exists():
        return int(path.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return None


async def measure(seconds: float) -> dict[str, object]:
    """Measure idle CPU and loop scheduling in a mounted Nova TUI."""
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.widgets import MatrixRain

    counts = {"status_ticks": 0, "rain_ticks": 0}
    status_tick, rain_tick = NovaApp._tick, MatrixRain._tick

    def status(app: NovaApp) -> None:
        counts["status_ticks"] += 1
        status_tick(app)

    def rain(widget: MatrixRain) -> None:
        counts["rain_ticks"] += 1
        rain_tick(widget)

    async def no_voice(_app: NovaApp) -> None:
        pass

    os.environ["NOVA_DISABLE_UPDATE_CHECK"] = "1"
    os.environ.pop("NO_COLOR", None)
    # Measure the TUI, not network, audio downloads or a user's saved layout.
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(NovaApp, "_eager_voice_warmup", no_voice),
        patch.object(NovaApp, "_tick", status),
        patch.object(MatrixRain, "_tick", rain),
        patch(
            "novacode_cli.tui.harness.profile_path", return_value=Path(directory) / "layout.json"
        ),
    ):
        app = NovaApp(
            agent=Agent(),
            assistant_id="benchmark",
            session_state=Session(),
            backend=None,
            token_tracker=None,
            image_tracker=None,
            model_name="benchmark",
        )
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await asyncio.sleep(1)  # exclude startup from the idle sample
            counts.update(status_ticks=0, rain_ticks=0)
            cpu, start = time.process_time(), time.monotonic()
            deadline = start + seconds
            delays = []
            while time.monotonic() < deadline:
                before = time.monotonic()
                await asyncio.sleep(0.02)
                delays.append(max(0.0, time.monotonic() - before - 0.02) * 1000)
            elapsed = time.monotonic() - start
            cpu_seconds = time.process_time() - cpu
            rss = working_set()
            ordered = sorted(delays)
            return {
                **counts,
                "wall_seconds": round(elapsed, 3),
                "cpu_seconds": round(cpu_seconds, 4),
                "cpu_one_core_pct": round(cpu_seconds / elapsed * 100, 2),
                "working_set_mib": round(rss / 1024**2, 2) if rss is not None else None,
                "loop_delay_p95_ms": round(ordered[int((len(ordered) - 1) * 0.95)], 3),
                "loop_delay_max_ms": round(max(delays), 3),
                "loop_delay_mean_ms": round(statistics.mean(delays), 3),
            }


def main() -> None:
    """Run simultaneous child sessions and collect measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.seconds < 1 or not 1 <= args.sessions <= MAX_SESSIONS:
        parser.error("use 1-4 sessions and at least one second")
    if args.child:
        print(json.dumps(asyncio.run(measure(args.seconds))))
        return
    processes = [
        subprocess.Popen(  # noqa: S603 — fixed script and validated numeric arguments
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                "--seconds",
                str(args.seconds),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        for _ in range(args.sessions)
    ]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=args.seconds + 90)
            if process.returncode:
                message = f"Benchmark child failed ({process.returncode}): {stderr}\n{stdout}"
                raise RuntimeError(message)
            results.append(json.loads(stdout.strip().splitlines()[-1]))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    print(json.dumps({"python": sys.version, "sessions": results}, indent=2))


if __name__ == "__main__":
    main()
