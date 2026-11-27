"""The Background Tasks panel's refresh mechanics.

Event-driven refresh (fixes the always-on 1 Hz clock), the incremental
OptionList diff (no rebuild + selection reset when a job finishes), and the
paused runtime ticker when the panel is empty.
"""

from __future__ import annotations

import time

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")

from novacode_cli.shell.jobs import BackgroundJob, JobRegistry  # noqa: E402
from novacode_cli.tui.screens import BackgroundTasksScreen  # noqa: E402


def _job(registry: JobRegistry, command: str = "uv run pytest -q") -> BackgroundJob:
    return registry.add(command, tool_name="tui-shell")


def _agent_task(task_id: str, status: str = "running"):
    """A minimal stand-in for the app's AgentTask rows."""

    class _Task:
        def __init__(self):
            self.task_id = task_id
            self.status = status
            self.command = "agent task"
            self.exit_code = None
            self.started_at = time.time()
            self.worker = None

        def status_glyph(self):
            return {"running": "▸", "done": "✓", "failed": "✗"}.get(self.status, "•")

        def runtime(self):
            return 1.0

    return _Task()


def _list(ol):
    return [str(option.prompt) for option in ol._options]


async def test_empty_panel_pauses_the_runtime_ticker():
    """No tasks: the 1 Hz runtime clock is paused; a later job resumes it."""
    registry = JobRegistry()
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = BackgroundTasksScreen(registry=registry)
        app._post_to_ui = lambda callback, *args: callback(*args)
        app.push_screen(screen)
        await pilot.pause()
        ticker = screen._ticker
        assert ticker is not None
        # Timer.pause()/resume() gate an event: not set == paused.
        assert not ticker._active.is_set(), "no rows: runtime ticker must pause"

        job = _job(registry)
        await pilot.pause(0.05)
        await pilot.pause(0.05)
        assert ticker._active.is_set(), "a task arrived: the ticker must resume"
        assert len(screen._tasks) == 1
        assert screen._row_option_ids == [job.task_id]

        registry.terminate(job.id, force=True)
        screen._flush_event_refresh()
        await pilot.pause()
        assert screen._tasks and screen._tasks[0].status != "running"


async def test_job_finishing_updates_rows_in_place_without_rebuild(monkeypatch):
    """A finish is one removal + one append: other rows keep their widgets."""
    registry = JobRegistry()
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = BackgroundTasksScreen(registry=registry)
        app._post_to_ui = lambda callback, *args: callback(*args)
        app.push_screen(screen)
        await pilot.pause()

        first = _job(registry, "first command")
        # The second event lands inside the 0.25 s refresh throttle; flush it.
        second = _job(registry, "second command")
        screen._flush_event_refresh()
        await pilot.pause()
        assert screen._row_option_ids == [first.task_id, second.task_id]

        ol = screen.query_one("#tasks-list")
        original_clear = type(ol).clear_options
        clear_count = []

        def spy_clear(self):
            clear_count.append(1)
            return original_clear(self)

        monkeypatch.setattr(type(ol), "clear_options", spy_clear)
        registry.complete(second.id, 0)
        await pilot.pause(0.05)
        await pilot.pause(0.05)

        assert screen._row_option_ids == [first.task_id, second.task_id]
        assert not clear_count, "a finish must not rebuild the list"
        prompts = _list(ol)
        assert first.task_id in prompts[0] and "running" in prompts[0]
        assert second.task_id in prompts[1] and "done" in prompts[1]


async def test_new_running_task_falls_back_to_rebuild_in_sorted_order():
    """A new *running* task sorts above existing rows: rebuild, correct order."""
    registry = JobRegistry()
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = BackgroundTasksScreen(registry=registry)
        app._post_to_ui = lambda callback, *args: callback(*args)
        app.push_screen(screen)
        await pilot.pause()

        done_job = _job(registry, "already done")
        registry.complete(done_job.id, 0)
        screen._flush_event_refresh()
        await pilot.pause()
        assert screen._row_option_ids == [done_job.task_id]

        late = _job(registry, "started later")
        screen._flush_event_refresh()
        await pilot.pause()
        assert screen._row_option_ids == [late.task_id, done_job.task_id]
        prompts = _list(screen.query_one("#tasks-list"))
        assert late.task_id in prompts[0], "the running job must sort first"


async def test_clear_done_removes_rows_without_rebuild(monkeypatch):
    registry = JobRegistry()
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = BackgroundTasksScreen(registry=registry)
        app._post_to_ui = lambda callback, *args: callback(*args)
        app.push_screen(screen)
        await pilot.pause()

        _first = _job(registry, "first")
        _second = _job(registry, "second")
        screen._flush_event_refresh()
        await pilot.pause()
        assert len(screen._row_option_ids) == 2

        ol = screen.query_one("#tasks-list")
        original_clear = type(ol).clear_options
        clear_count = []

        def spy_clear(self):
            clear_count.append(1)
            return original_clear(self)

        # "Clear done" only removes non-running jobs: finish both first.
        # (The completions themselves may rebuild — a running→done flip
        # reorders the sorted rows, which cannot be expressed in place.)
        registry.complete(_first.id, 0)
        registry.complete(_second.id, 0)
        screen._flush_event_refresh()
        await pilot.pause()
        assert len(screen._row_option_ids) == 2
        clears_before = len(clear_count)

        monkeypatch.setattr(type(ol), "clear_options", spy_clear)
        registry.clear_completed()
        screen._flush_event_refresh()
        await pilot.pause()

        assert screen._row_option_ids == ["_"], "empty panel: only the placeholder remains"
        assert len(clear_count) == clears_before, "pure removals must not rebuild the list"
        assert screen.query_one("#tasks-list").option_count == 1  # the placeholder


async def test_agent_extra_tasks_appear_and_disappear():
    registry = JobRegistry()
    app = _app()
    state = {"tasks": [_agent_task("agent-1")]}

    async with app.run_test(size=(120, 40)) as pilot:
        screen = BackgroundTasksScreen(registry=registry, extra_tasks=lambda: state["tasks"])
        app._post_to_ui = lambda callback, *args: callback(*args)
        app.push_screen(screen)
        await pilot.pause()
        await pilot.pause(0.05)
        assert screen._row_option_ids == ["agent-1"]

        state["tasks"] = []
        screen._refresh()
        assert screen._row_option_ids == ["_"], "empty panel shows the placeholder"


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    class _FakeAgent:
        async def aget_state(self, config):
            class _State:
                values = {"messages": []}

            return _State()

    class _SS:
        thread_id = "t1"
        session_id = "s1"
        auto_approve = False
        plan_mode_enabled = False
        todos: list = []
        steering_instructions: list = []

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
