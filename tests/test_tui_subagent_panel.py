"""The dynamic-subagents panel, driven through the real app.

The panel exists to make a fan-out legible while it runs: which phase a task
belongs to, what it is doing, on what model, for how long, and why it failed. It
is fed by ``SubagentTask`` events, so these tests drive those rather than a live
model call.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from novacode_cli import ui_events as ev

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


@pytest.fixture(autouse=True)
def _isolate_panel_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    if _HAS_TEXTUAL:
        from novacode_cli.tui.app import NovaApp

        async def no_warmup(self: object) -> None:
            pass

        monkeypatch.setattr(NovaApp, "_eager_voice_warmup", no_warmup)


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
        app._paint_subagents()
        out["running_title"] = str(app.query_one("#subagents-title").render())
        out["running_body"] = str(app.query_one("#subagents-body").render())
        # Finished and failed tasks leave the live panel.
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


async def _seconds_until_visible(dock, *, deadline: float) -> float | None:
    """Sample the dock's classes while time passes, the way a live turn does.

    Deliberately no ``pilot.pause()`` and no sleep-for-the-paint-timer: the point is
    to measure when the panel paints by itself, not to give it a chance to.
    """
    t0 = time.time()
    while time.time() - t0 < deadline:
        await asyncio.sleep(0.02)
        if dock.has_class("active"):
            return time.time() - t0
    return None


def test_a_running_row_is_visible_before_the_turn_ends():
    """The panel is for reading *while* work runs, so assert the timing, not the result.

    The other tests in this file sleep 0.15s (and in one case feed every completion)
    before asserting, which also passes if the panel only ever paints once the turn is
    over. This one asserts the panel becomes visible on its own, from a running row,
    with no Done event and nothing else to trigger it, while the row is still running.
    """
    if not _HAS_TEXTUAL:
        return

    async def _run() -> tuple[float | None, bool, str | None, str]:
        app = _app()
        async with app.run_test(size=(120, 44)) as pilot:
            for _ in range(4):
                await pilot.pause()
            dock = app.query_one("#subagents-dock")
            assert not dock.has_class("active"), "the panel was visible before any dispatch"

            await app._render(
                ev.SubagentTask(
                    task_id="t1", status="running", label="research: one", started_at=time.time()
                )
            )
            visible_after = await _seconds_until_visible(dock, deadline=2.0)
            still_running = app._subagent_rows["t1"].status == "running"
            collapsed = dock.has_class("collapsed")
            title = str(app.query_one("#subagents-title").render())
        return visible_after, still_running, collapsed, title

    visible_after, still_running, collapsed, title = asyncio.run(_run())

    assert visible_after is not None, (
        "the panel never became visible while a row was running: it is only appearing "
        "once the work is done"
    )
    assert still_running, "the panel only painted after the row had already finished"
    assert not collapsed, f"the panel painted folded shut while work was in flight: {title!r}"


def test_a_fan_out_becomes_one_phase_with_a_row_per_task():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_a_fan_out())

    assert out["docks"] == 1, "the panel mounted more than once"
    assert not out["active"], "the panel remained visible after all tasks finished"
    assert out["title"] == ""
    assert out["body"] == ""
    assert "5 running" in out["running_title"], out["running_title"]
    assert "1 phase" in out["running_title"], out["running_title"]
    # One phase, six tasks, each named.
    assert out["phases"] == ["call_eval_1"]
    assert "research: docling-0" in out["running_body"]
    # The type must not be prefixed on top of the label's own kind prefix.
    assert "researcher: research:" not in out["running_body"], out["running_body"]
    assert "research: docling-4" in out["running_body"]
    assert "chroma" not in out["running_body"]
    # The MODEL column names what the row will actually run on. Since per-role models
    # landed, a row resolves through the role resolver rather than echoing the session
    # model, so it renders as the role's `provider:model` when that role is set. Assert
    # against the resolver instead of a literal, or this test only passes on a machine
    # whose subagent role is unset. Blank is the failure that matters.
    from novacode_cli.config.role_models import panel_row_model

    assert out["model"], "the MODEL column was blank"
    assert out["model"] == panel_row_model("eval", "deepseek-v4.1-flash"), out["model"]
    # The dock is hidden after the final completion.
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
    assert "auditor" not in out["body"]


async def _drive_a_dropped_paint_timer() -> dict:
    """A coalescing timer that is dropped must not wedge the panel.

    Textual runs a ``set_timer`` callback as a ``call_next`` job, so it is lost if
    the app stops draining those (the callback never runs, and the handle it would
    have cleared stays set). Gating the coalescer on that handle means one dropped
    timer silently disables every later repaint, for the rest of the session.
    """
    out: dict = {}
    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        for _ in range(4):
            await pilot.pause()
        dock = app.query_one("#subagents-dock")
        await app._render(_task("first", status="running", label="research: one"))
        # Drop the pending one-shot the way Textual does when it stops
        # processing messages: the timer is stopped and its callback never runs.
        timer = app._subagents_paint_timer
        out["had_timer"] = timer is not None
        if timer is not None:
            timer.stop()
        # Past the coalescing window, so a later dispatch is a *new* schedule.
        await asyncio.sleep(0.2)
        await pilot.pause()
        out["painted_after_drop"] = dock.has_class("active")

        await app._render(_task("second", status="running", label="research: two"))
        await asyncio.sleep(0.3)
        await pilot.pause()
        out["active"] = dock.has_class("active")
        out["body"] = str(app.query_one("#subagents-body").render())
    return out


def test_a_dropped_paint_timer_does_not_wedge_the_panel():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_a_dropped_paint_timer())

    assert out["had_timer"], "the first dispatch scheduled no coalescing timer at all"
    assert out["active"], (
        "a dropped coalescing timer left the panel permanently unpainted: the "
        "next dispatch could not schedule a repaint"
    )
    assert "research: two" in out["body"], out["body"]


@pytest.mark.parametrize("event", [ev.Cancelled(), ev.Error("provider stopped"), ev.Done()])
async def test_turn_end_stops_foreground_rows_and_freezes_time(
    event: ev.Cancelled | ev.Error | ev.Done,
) -> None:
    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        await app._render(_task("direct", phase_kind="direct", started_at=time.time() - 5))
        await app._render(_task("fanout", phase_kind="eval", started_at=time.time() - 4))
        await app._render(_task("finished", status="done", duration_ms=3000))
        app._paint_subagents()
        assert app._subagents_tick is not None

        await app._render(event)
        for task_id in ("direct", "fanout"):
            row = app._subagent_rows[task_id]
            assert row.status == "stopped"
            assert row.duration_ms is not None
            assert row.duration_ms > 0
        assert app._subagent_rows["finished"].status == "done"
        assert app._subagent_rows["finished"].duration_ms == 3000
        assert app._subagents_tick is None
        assert app._subagents_stale
        durations = {key: row.duration_ms for key, row in app._subagent_rows.items()}
        await app._render(ev.Cancelled())
        assert {key: row.duration_ms for key, row in app._subagent_rows.items()} == durations
        title = str(app.query_one("#subagents-title").render())
        assert title == ""
        assert not app.query_one("#subagents-dock").has_class("active")
        before = str(app.query_one("#subagents-body").render())
        assert before == ""
        await asyncio.sleep(1.1)
        await pilot.pause()
        assert str(app.query_one("#subagents-body").render()).split() == before.split()

        await app._render(_task("next", started_at=time.time()))
        app._paint_subagents()
        assert list(app._subagent_rows) == ["next"]
        assert app._subagents_tick is not None


async def test_interrupt_keeps_independent_async_rows_running():
    app = _app()
    async with app.run_test(size=(120, 44)):
        await app._render(_task("sync", phase_kind="direct", started_at=time.time()))
        app._ingest_async_tasks({"a": {"task_id": "a", "status": "running"}})
        await app._render(ev.Cancelled())
        assert app._subagent_rows["sync"].status == "stopped"
        assert app._subagent_rows["async:a"].status == "running"
        assert app._subagents_tick is not None


@pytest.mark.parametrize("status", ["done", "failed", "stopped"])
async def test_terminal_rows_disappear_while_other_tasks_stay_visible(status: str):
    app = _app()
    async with app.run_test(size=(120, 44)):
        await app._render(_task("finishing", label="finishing task"))
        await app._render(_task("ongoing", label="ongoing task"))
        await app._render(_task("finishing", status=status))
        app._paint_subagents()
        assert [row.task_id for row in app._subagent_task_list()] == ["ongoing"]
        body = str(app.query_one("#subagents-body").render())
        assert "ongoing task" in body
        assert "finishing task" not in body
        assert app._subagents_tick is not None


async def test_running_async_task_survives_new_foreground_dispatch():
    app = _app()
    async with app.run_test(size=(120, 44)):
        app._ingest_async_tasks(
            {"a": {"task_id": "a", "agent_name": "reviewer", "status": "running"}}
        )
        await app._render(ev.Done(had_response=True))
        await app._render(_task("new", label="new task"))
        app._paint_subagents()
        assert {row.task_id for row in app._subagent_task_list()} == {"async:a", "new"}
        app._ingest_async_tasks({"a": {"task_id": "a", "status": "success"}})
        app._paint_subagents()
        assert [row.task_id for row in app._subagent_task_list()] == ["new"]
        await app._render(_task("new", status="done"))
        app._paint_subagents()
        assert not app.query_one("#subagents-dock").has_class("active")
        assert app._subagents_tick is None


async def test_async_dispatch_expands_panel_after_completed_foreground_turn():
    app = _app()
    async with app.run_test(size=(120, 44)):
        await app._render(_task("old", status="done"))
        app._ingest_async_tasks({"a": {"task_id": "a", "status": "running"}})
        app._paint_subagents()
        assert app.query_one("#subagents-dock").has_class("active")
        assert not app.query_one("#subagents-dock").has_class("collapsed")


async def test_clear_removes_panel_rows_phases_and_timers(monkeypatch: pytest.MonkeyPatch):
    app = _app()

    async def no_learning() -> None:
        pass

    monkeypatch.setattr(app, "_consolidate_learning", no_learning)
    async with app.run_test(size=(120, 44)) as pilot:
        await app._render(_task("old", phase_id="phase", started_at=time.time()))
        app._paint_subagents()
        await app._render(_task("pending", started_at=time.time()))
        assert app._subagents_tick is not None
        assert app._subagents_paint_timer is not None
        await app._run_clear()
        await pilot.pause()
        assert app._subagent_rows == {}
        assert app._subagent_phase_order == []
        assert app._subagent_collapsed_phases == set()
        assert app._subagents_rows_by_line == []
        assert app._subagents_tick is None
        assert app._subagents_paint_timer is None
        assert not app.query_one("#subagents-dock").has_class("active")
        assert str(app.query_one("#subagents-title").render()) == ""
        assert str(app.query_one("#subagents-body").render()) == ""


async def test_escape_cancels_stream_and_stops_subagent_panel():
    app = _app()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingAgent(type(app.agent)):
        async def astream(self, _inp, **_kw):  # noqa: ANN001, ANN003, ANN202
            yield (
                (),
                "custom",
                {
                    "type": "subagent",
                    "phase": "start",
                    "id": "live",
                    "label": "sync task",
                    "subagent_type": "researcher",
                },
            )
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    app.agent = HangingAgent()
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        app._dispatch("run a subagent")
        await asyncio.wait_for(started.wait(), timeout=5)
        assert app._subagent_rows["live"].status == "running"
        await pilot.press("escape")
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        for _ in range(50):
            await pilot.pause()
            if not app._turn_active:
                break
        assert not app._turn_active
        assert app._subagent_rows["live"].status == "stopped"
        assert app._subagents_tick is None


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_worker_cleanup_stops_rows_without_a_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    app = _app()

    async def interrupted_stream(_text: str, _assistant_id: str | None) -> None:
        await app._render(_task("unfinished", phase_kind="direct", started_at=time.time()))
        raise failure()

    monkeypatch.setattr(app, "_do_stream", interrupted_stream)
    async with app.run_test(size=(120, 44)) as pilot:
        app._dispatch("start")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app._turn_active
        assert app._subagent_rows["unfinished"].status == "stopped"
        assert app._subagent_rows["unfinished"].duration_ms is not None
        assert app._subagents_tick is None
