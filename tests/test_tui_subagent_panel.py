"""The dynamic-subagents panel, driven through the real app.

The panel exists to make a fan-out legible while it runs: which phase a task
belongs to, what it is doing, on what model, for how long, and why it failed. It
is fed by ``SubagentTask`` events, so these tests drive those rather than a live
model call.
"""

from __future__ import annotations

import asyncio
import time

from novacode_cli import ui_events as ev

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _FakeAgent, _SS

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="deepseek-v4.1-flash",
    )


def _task(task_id: str, **kwargs) -> ev.SubagentTask:
    base: dict[str, object] = {
        "task_id": task_id,
        "status": "running",
        "label": "research: docling",
        "started_at": 0.0,
    }
    base.update(kwargs)
    return ev.SubagentTask(**base)  # type: ignore[arg-type]


async def _drive_a_fan_out() -> dict:
    """Six tasks in one phase, one of them failing, then read the panel."""
    out: dict = {}
    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        for _ in range(4):
            await pilot.pause()
        out["docks"] = len(app.query("#subagents-dock"))

        for index in range(5):
            await app._render(
                ev.SubagentTask(
                    task_id=f"ptc_task_{index}",
                    status="running",
                    phase_id="call_eval_1",
                    phase_kind="eval",
                    subagent_type="researcher",
                    label=f"research: docling-{index}",
                    started_at=100.0,
                    model=None,
                )
            )
        await app._render(
            _task(
                "ptc_task_fail",
                status="failed",
                phase_id="call_eval_1",
                subagent_type="researcher",
                label="research: chroma",
                error="'charmap' codec can't decode byte 0x8f in position 3716",
                duration_ms=33_600,
            )
        )
        # Five finish, so the header should read 5/6 done · 1 phase · 1 failed.
        for index in range(5):
            await app._render(
                ev.SubagentTask(
                    task_id=f"ptc_task_{index}",
                    status="done",
                    phase_id="call_eval_1",
                    duration_ms=38_300,
                )
            )
        await asyncio.sleep(0.15)
        await pilot.pause()

        dock = app.query_one("#subagents-dock")
        out["active"] = dock.has_class("active")
        out["title"] = str(app.query_one("#subagents-title").render())
        out["body"] = str(app.query_one("#subagents-body").render())
        out["collapsed_after_all_done"] = dock.has_class("collapsed")
        out["rows"] = dict(app._subagent_rows)
        out["model"] = app._subagent_rows["ptc_task_0"].model
        out["phases"] = list(app._subagent_phase_order)
    return out


def test_a_fan_out_becomes_one_phase_with_a_row_per_task():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_a_fan_out())

    assert out["docks"] == 1, "the panel mounted more than once"
    assert out["active"], "the panel never became visible"
    assert "5/6 done" in out["title"], out["title"]
    assert "1 phase" in out["title"], out["title"]
    assert "1 failed" in out["title"], out["title"]
    # One phase, six tasks, each named.
    assert out["phases"] == ["call_eval_1"]
    assert "research: docling-0" in out["body"]
    # The type must not be prefixed on top of the label's own kind prefix.
    assert "researcher: research:" not in out["body"], out["body"]
    assert "research: docling-4" in out["body"]
    # The failure text is the reason the row exists.
    assert "charmap" in out["body"]
    assert "33.6s" in out["body"]
    # Subagents inherit the session model, so the column is not blank.
    assert out["model"] == "deepseek-v4.1-flash"
    # And a finished run folds down to its header rather than holding rows.
    assert out["collapsed_after_all_done"]


async def _drive_live_time_and_collapse() -> dict:
    out: dict = {}
    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await app._render(
            ev.SubagentTask(
                task_id="live",
                status="running",
                phase_id="call_eval_9",
                label="research: slow",
                started_at=time.time(),
            )
        )
        await app._render(
            _task("ptc_other", status="running", phase_id="call_eval_9", started_at=time.time())
        )

        # The panel repaints on a 0.1s coalescing timer (a fan-out arrives in a
        # burst), so let it land before reading the first frame.
        body = app.query_one("#subagents-body")
        for _ in range(20):
            await asyncio.sleep(0.02)
            await pilot.pause()
            if str(body.render()).strip():
                break
        out["first_frame"] = str(body.render())
        # A running row's TIME ages: the panel repaints on its own.
        await asyncio.sleep(1.25)
        await pilot.pause()
        out["later_frame"] = str(body.render())
        out["ticking"] = app._subagents_tick is not None

        # alt+s folds the panel; the body goes away with the class.
        app.action_toggle_subagents()
        await pilot.pause()
        out["collapsed_class"] = app.query_one("#subagents-dock").has_class("collapsed")
        app.action_toggle_subagents()
        await pilot.pause()
        out["expanded_again"] = not app.query_one("#subagents-dock").has_class("collapsed")

        # Clicking a phase line folds that phase only. Dock line 0 is the header
        # text, line 1 the table header, line 2 the first phase.
        out["lines"] = len(app._subagents_rows_by_line)
        app._on_subagents_click(2)
        await pilot.pause()
        out["folded_phase_body"] = str(app.query_one("#subagents-body").render())
        out["folded_phases"] = set(app._subagent_collapsed_phases)
        app._on_subagents_click(2)
        await pilot.pause()
        out["unfolded_phases"] = set(app._subagent_collapsed_phases)
    return out


def test_the_panel_ticks_while_running_and_folds_on_the_key_and_a_click():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_live_time_and_collapse())

    timeline = out["first_frame"]
    assert "⏳" in timeline
    assert out["first_frame"] != out["later_frame"], (
        "a running row's TIME never moved: the panel is not ticking"
    )
    assert out["ticking"], "no ticker was started while a task was running"
    assert out["collapsed_class"], "alt+s did not fold the panel"
    assert out["expanded_again"], "alt+s did not unfold it again"
    # A click on the phase line folds that phase's tasks, and only that phase's.
    assert out["folded_phases"] == {"call_eval_9"}
    assert "research: slow" not in out["folded_phase_body"], out["folded_phase_body"]
    assert out["unfolded_phases"] == set()


async def _drive_a_new_turn_replaces_the_last_run() -> dict:
    out: dict = {}
    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await app._render(_task("old-1", status="done", duration_ms=1000))
        await app._render(ev.Done(had_response=True))
        await pilot.pause()
        out["after_turn"] = list(app._subagent_rows)
        out["stale"] = app._subagents_stale

        # The next turn's first dispatch takes the panel over.
        await app._render(_task("new-1", status="running"))
        await asyncio.sleep(0.15)
        await pilot.pause()
        out["after_new_turn"] = list(app._subagent_rows)
        out["stale_after"] = app._subagents_stale
        out["collapsed"] = app.query_one("#subagents-dock").has_class("collapsed")
    return out


def test_a_new_turn_replaces_the_previous_runs_rows():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_a_new_turn_replaces_the_last_run())

    assert out["after_turn"] == ["old-1"]
    assert out["stale"], "the turn end was not recorded"
    assert out["after_new_turn"] == ["new-1"], out["after_new_turn"]
    assert not out["stale_after"]
    assert not out["collapsed"], "a running row must not leave the panel folded"


async def _drive_async_rows() -> dict:
    out: dict = {}
    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        for _ in range(4):
            await pilot.pause()
        app._ingest_async_tasks(
            {
                "t1": {"task_id": "t1", "agent_name": "doc-writer", "status": "running"},
                "t2": {"task_id": "t2", "agent_name": "auditor", "status": "success"},
            }
        )
        await asyncio.sleep(0.15)
        await pilot.pause()
        out["rows"] = sorted(app._subagent_rows)
        out["body"] = str(app.query_one("#subagents-body").render())
        out["phases"] = list(app._subagent_phase_order)
    return out


def test_remote_async_tasks_show_in_the_same_panel():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_async_rows())

    assert out["rows"] == ["async:t1", "async:t2"], out["rows"]
    assert out["phases"] == ["async"]
    assert "doc-writer" in out["body"]
    assert "auditor" in out["body"]
