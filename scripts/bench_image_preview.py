"""Measure the image preview's frame cost with the rain running vs stopped.

Run: uv run python scripts/bench_image_preview.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from PIL import Image

from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.screens import ImagePreviewScreen
from novacode_cli.ui.ui_elements import TokenTracker

TICK_MS = 16


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
    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )


class Probe:
    def __init__(self, app):
        self.gaps: list[float] = []
        self._last = time.perf_counter()
        self._timer = app.set_interval(TICK_MS / 1000, self._tick)

    def _tick(self) -> None:
        now = time.perf_counter()
        self.gaps.append((now - self._last) * 1000)
        self._last = now

    def summary(self) -> dict:
        gaps = sorted(self.gaps)
        return {
            "n": len(gaps),
            "p50": round(statistics.median(gaps), 2),
            "p95": round(gaps[int(len(gaps) * 0.95)], 2),
            "p99": round(gaps[int(len(gaps) * 0.99)], 2),
            "max": round(gaps[-1], 2),
            "stalls>50ms": sum(1 for g in gaps if g > 50),
        }


async def main() -> None:
    import tempfile
    from pathlib import Path

    import novacode_cli.config.config as config_module
    from novacode_cli.config.nova_config import NovaConfig

    tmp = Path(tempfile.mkdtemp())
    config_module.HOME_DIR = tmp / ".nova"

    async def no_warmup():
        pass

    def initialize(config):
        config.config_dir = tmp / ".nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {"home_style": "classic"}
        config._loaded = {}

    NovaConfig.__init__ = initialize
    NovaApp._eager_voice_warmup = lambda self: no_warmup()

    shot = tmp / "step-final.png"
    Image.new("RGB", (2400, 1500), (30, 30, 60)).save(shot)

    from test_tui_sessions import _app as make_app

    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()
        rain = app._home_banner

        results = {}
        # Interleave the two phases twice and keep the second pass, so one-time
        # costs (image load, mount, style warm-up) do not bias the first phase.
        for round_index in range(2):
            for label, pause_rain in (("rain-running", False), ("rain-paused", True)):
                if rain is not None:
                    (rain.pause if pause_rain else rain.resume)()
                frames_before = getattr(rain, "_frame_count", 0)
                probe = Probe(app)
                screen = ImagePreviewScreen(str(shot))
                app.push_screen(screen)
                for _ in range(80):
                    await pilot.pause()
                scroll = screen.query_one("#image-scroll")
                for _ in range(20):
                    scroll.scroll_down()
                    await pilot.pause()
                if round_index == 1:
                    results[label] = probe.summary()
                    results[label]["rain_frames_while_open"] = (
                        getattr(rain, "_frame_count", 0) - frames_before
                    )
                app.pop_screen()
                for _ in range(10):
                    await pilot.pause()
        print("half-block path, 2400x1500 image, 160x40 grid, second pass")
        for label, data in results.items():
            print(f"  {label}: {data}")


asyncio.run(main())
