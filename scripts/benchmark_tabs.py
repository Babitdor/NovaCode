"""Measure real tab rendering without network/model calls or user configuration.

Run: python scripts/benchmark_tabs.py --tabs 4 --events 2000 --history 100
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import os
import pstats
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def measure(tabs: int, events: int, history: int) -> dict:
    from textual.widgets import ContentSwitcher

    from novacode_cli import ui_events as ev
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.session_pane import SessionPane, fresh_state
    from novacode_cli.tui.widgets import TranscriptScroll

    class Agent:
        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": []})

    def session(sid):
        return SimpleNamespace(
            thread_id=sid,
            session_id=sid,
            auto_approve=False,
            plan_mode_enabled=False,
            todos=None,
            steering_instructions=[],
        )

    def configure(config):
        config.config_dir = Path(directory)
        config.config_path = Path(directory) / "Nova.config.json"
        config._config = {}
        config._loaded = {}

    async def no_voice(_app):
        pass

    os.environ["NOVA_DISABLE_UPDATE_CHECK"] = "1"
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(NovaConfig, "__init__", configure),
        patch.object(NovaApp, "_eager_voice_warmup", no_voice),
        patch.object(NovaApp, "_refresh_branch_worker", lambda _: None),
        patch("novacode_cli.config.config.HOME_DIR", Path(directory)),
        patch(
            "novacode_cli.tui.harness.profile_path", return_value=Path(directory) / "layout.json"
        ),
    ):
        app = NovaApp(
            agent=Agent(),
            assistant_id="benchmark",
            session_state=session("root"),
            backend=None,
            token_tracker=None,
            image_tracker=None,
            model_name="benchmark",
        )
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            for i in range(1, tabs):
                scroll = TranscriptScroll(id=f"pane-bench-{i}")
                await app.query_one("#panes", ContentSwitcher).mount(scroll)
                scroll.display = False
                pane = SessionPane(
                    sid=f"bench-{i}", title=f"Project {i}", scroll=scroll, kind="child"
                )
                pane.state = fresh_state(session_state=session(pane.sid), model_name="benchmark")
                app._panes.append(pane)
            app._refresh_tabs()
            await pilot.pause()
            refreshes = 0
            refresh = app._refresh_tabs

            def counted_refresh():
                nonlocal refreshes
                refreshes += 1
                refresh()

            app._refresh_tabs = counted_refresh
            delays = []
            running = True

            async def heartbeat():
                while running:
                    before = time.perf_counter()
                    await asyncio.sleep(0.01)
                    delays.append(max(0, time.perf_counter() - before - 0.01) * 1000)

            ticker = asyncio.create_task(heartbeat())
            await asyncio.sleep(0)
            started, cpu = time.perf_counter(), time.process_time()
            children = app._panes[1:]
            for i in range(events):
                if children:
                    await app._on_child_message(
                        children[i % len(children)].sid,
                        {"t": "ev", "c": "TextDelta", "d": {"text": "token "}},
                    )
                else:
                    await app._deliver(app._root_pane, ev.TextDelta(text="token "))
            burst_ms = (time.perf_counter() - started) * 1000
            cpu_ms = (time.process_time() - cpu) * 1000
            event_refreshes = refreshes
            switch_ms, remaining = None, 0
            if children:
                pane = children[0]
                for i in range(history):
                    await app._deliver(
                        pane,
                        ev.AssistantMessage(
                            text=f"History {i}: investigation result.",
                            agent_name="nova",
                            agent_color="cyan",
                        ),
                    )
                started = time.perf_counter()
                await app._switch_to(pane)
                switch_ms = (time.perf_counter() - started) * 1000
                remaining = len(pane.buffer)
                deadline = time.monotonic() + 30
                while pane.buffer and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                if pane.buffer:
                    raise RuntimeError("History replay did not finish")
            await pilot.pause()
            running = False
            await ticker
            return {
                "tabs": tabs,
                "events": events,
                "history": history,
                "burst_ms": round(burst_ms, 2),
                "burst_cpu_ms": round(cpu_ms, 2),
                "tab_refresh_calls_during_burst": event_refreshes,
                "switch_ms": round(switch_ms, 2) if switch_ms is not None else None,
                "history_remaining_when_switch_returned": remaining,
                "loop_delay_max_ms": round(max(delays, default=0), 2),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tabs", type=int, choices=range(1, 6), default=4)
    parser.add_argument("--events", type=int, default=2000)
    parser.add_argument("--history", type=int, default=100)
    parser.add_argument("--profile", type=Path, help="Write a cProfile recording for diagnosis")
    args = parser.parse_args()
    if not 1 <= args.events <= 100000 or not 1 <= args.history <= 2000:
        parser.error("events must be 1–100000 and history must be 1–2000")
    profile = cProfile.Profile() if args.profile else None
    if profile:
        profile.enable()
    result = asyncio.run(measure(args.tabs, args.events, args.history))
    if profile:
        profile.disable()
        args.profile.parent.mkdir(parents=True, exist_ok=True)
        profile.dump_stats(args.profile)
    print(json.dumps(result, indent=2))
    if profile:
        pstats.Stats(profile).strip_dirs().sort_stats("cumulative").print_stats(
            "render|layout|_replay", 20
        )


if __name__ == "__main__":
    main()
