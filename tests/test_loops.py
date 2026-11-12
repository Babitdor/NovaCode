"""Timing, ownership and permission-path contracts for session-only loops."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.loops import (
    DEFAULT_PROMPT,
    MAX_TTL,
    LoopJob,
    LoopScheduler,
    LoopSpec,
    current_loop,
    duration,
    load_instructions,
    loop_command,
    next_cron,
    parse_loop,
)
from novacode_cli.tools.loop_tools import build_loop_tools
from novacode_cli.tui.loop_commands import (
    close_loops,
    loop_idle,
    poll_loops,
    run_loop_command,
    run_loop_iteration,
)


@pytest.mark.parametrize(("text", "seconds"), [("30s", 30), ("5m", 300), ("2h", 7200)])
def test_duration(text: str, seconds: int):
    assert duration(text) == seconds


@pytest.mark.parametrize("text", ["0s", "-1m", "NaNh", "infs", "0.1s", "5", "5ms"])
def test_invalid_duration(text: str):
    with pytest.raises(ValueError, match=r".+"):
        duration(text)


def test_modes_and_prompt_preservation():
    assert parse_loop("") == LoopSpec()
    assert parse_loop("5m").interval == 300
    assert parse_loop("watch deployment").prompt == "watch deployment"
    spec = parse_loop('5m check "a b" and C:\\repo')
    assert spec.prompt == 'check "a b" and C:\\repo'
    assert parse_loop("1h /review-pr 128").prompt == "/review-pr 128"
    cron = parse_loop('"*/15 * * * *" check staging')
    assert cron.cron == "*/15 * * * *"
    assert cron.prompt == "check staging"
    assert not parse_loop('"0 9 * * *"').prompt
    assert parse_loop("--ttl 7d 30s watch").ttl == MAX_TTL


@pytest.mark.parametrize(
    "text", ["--ttl 8d watch", "--ttl 0s", "5ms watch", '"* *" watch', '"unfinished']
)
def test_invalid_schedules(text: str):
    with pytest.raises(ValueError, match=r".+"):
        parse_loop(text)


def test_cron_boundaries_and_sunday_range():
    start = datetime(2026, 11, 12, 10, 14, 59, tzinfo=UTC)
    assert next_cron("*/15 * * * *", start) == datetime(2026, 11, 12, 10, 15, tzinfo=UTC)
    assert next_cron("0 9 * * 1-7", start) == datetime(2026, 11, 13, 9, tzinfo=UTC)
    assert next_cron("0 9 * * 7", start) == datetime(2026, 11, 15, 9, tzinfo=UTC)
    assert next_cron("0/15 * * * *", start) == datetime(2026, 11, 12, 10, 15, tzinfo=UTC)
    # With both day fields restricted, either matching is sufficient.
    assert next_cron("0 9 13 * 1", start) == datetime(2026, 11, 13, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match=r".+"):
        next_cron("0 0 31 2 *", start)
    with pytest.raises(ValueError, match="empty list"):
        next_cron("* * * * *,", start)


def test_file_precedence_reload_alias_and_bounds(tmp_path: Path):
    project, home = tmp_path / "project", tmp_path / "home"
    (home / ".nova").mkdir(parents=True)
    (home / ".nova/loop.md").write_text("user", encoding="utf-8")
    assert load_instructions(str(project), home) == "user"
    (project / ".agent").mkdir(parents=True)
    (project / ".agent/loop.md").write_text("alias", encoding="utf-8")
    assert load_instructions(str(project), home) == "alias"
    (project / ".nova").mkdir()
    path = project / ".nova/loop.md"
    path.write_text("project", encoding="utf-8")
    assert load_instructions(str(project), home) == "project"
    path.write_text("updated", encoding="utf-8")
    assert load_instructions(str(project), home) == "updated"
    path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceed"):
        load_instructions(str(project), home)
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_instructions(str(project), home)
    assert load_instructions("", tmp_path / "absent") == DEFAULT_PROMPT


def test_idle_gate_coalesces_busy_ticks_and_never_overlaps():
    scheduler = LoopScheduler("thread")
    job = scheduler.add(LoopSpec("check", interval=10), now=0)
    assert scheduler.take(idle=False, now=100) is None
    assert scheduler.take(idle=True, now=100) is job
    assert job.due == 110
    assert scheduler.take(idle=True, now=200) is None
    scheduler.finish(job)
    assert scheduler.take(idle=True, now=200) is job
    scheduler.finish(job)
    assert scheduler.take(idle=True, now=200) is None


def test_expiration_is_hard_and_emitted_once():
    scheduler = LoopScheduler("thread")
    job = scheduler.add(LoopSpec("watch", ttl=10), now=0)
    assert scheduler.take(idle=True, now=0) is job
    assert scheduler.expire(now=10) == [job.id]
    assert scheduler.expire(now=11) == []
    assert scheduler.wakeup(job.id, 60)["error"]
    scheduler.finish(job)
    assert not scheduler.jobs


def test_dynamic_requires_explicit_next_wakeup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("novacode_cli.loops.time.monotonic", lambda: 100)
    scheduler = LoopScheduler("thread")
    job = scheduler.add(LoopSpec("watch"))
    assert scheduler.take(idle=True) is job
    assert scheduler.wakeup(job.id, 30)["delay_seconds"] == 30
    scheduler.finish(job)
    assert scheduler.take(idle=True, now=129) is None
    assert scheduler.take(idle=True, now=130) is job
    scheduler.finish(job)
    assert not scheduler.jobs


def test_commands_ownership_listing_and_stop():
    state = SimpleNamespace(thread_id="one", _loop_host=True, headless=False)
    assert "scheduled" in loop_command(state, "5m watch")
    assert "watch" in loop_command(state, "list")
    assert "Stopped 1" in loop_command(state, "stop")
    loop_command(state, "watch")
    previous = state._loop_scheduler
    state.thread_id = "two"
    assert "No active" in loop_command(state, "status")
    assert previous.closed
    loop_command(state, "5m check")
    assert state._loop_scheduler.thread_id == "two"
    state.headless = True
    assert "interactive" in loop_command(state, "check")


@pytest.mark.asyncio
async def test_wakeup_tool_cannot_create_extend_or_cross_conversations():
    state = SimpleNamespace(thread_id="one", _loop_scheduler=LoopScheduler("one"))
    tool = build_loop_tools(state)[0]
    assert (await tool.ainvoke({"stop": True}))["error"]
    first = state._loop_scheduler.add(LoopSpec("first"))
    second = state._loop_scheduler.add(LoopSpec("second"))
    ambiguous = await tool.ainvoke({"stop": True})
    assert ambiguous["loop_ids"] == [first.id, second.id]
    assert ambiguous.get("loops") == [
        {"loop_id": first.id, "prompt": "first"},
        {"loop_id": second.id, "prompt": "second"},
    ]
    token = current_loop.set(("one", first.id))
    try:
        assert (await tool.ainvoke({"delay_seconds": 12}))["loop_id"] == first.id
        assert first.expires - first.due > 0
        assert (await tool.ainvoke({"delay_seconds": float("inf")}))["error"]
        assert (await tool.ainvoke({"stop": True}))["stopped"]
    finally:
        current_loop.reset(token)
    state.thread_id = "another"
    assert (await tool.ainvoke({"loop_id": second.id, "stop": True}))["error"]


def host(tmp_path: Path):
    state = SimpleNamespace(
        thread_id="one",
        _loop_host=True,
        headless=False,
        workspace_root=str(tmp_path),
        _loop_scheduler=LoopScheduler("one"),
        add_notification=Mock(),
    )
    app = SimpleNamespace(
        session_state=state,
        assistant_id="agent",
        _active_pane=SimpleNamespace(kind="root"),
        _turn_active=False,
        _loop_states=[state],
        _log=Mock(),
        _stream_prompt=AsyncMock(),
        _run_loop_job=Mock(),
        action_cancel_turn=Mock(),
    )
    return app, state


@pytest.mark.asyncio
async def test_iteration_uses_normal_turn_and_hot_reloads(tmp_path: Path):
    app, state = host(tmp_path)
    (tmp_path / ".nova").mkdir()
    path = tmp_path / ".nova/loop.md"
    path.write_text("inspect CI", encoding="utf-8")
    job = state._loop_scheduler.add(LoopSpec(interval=10))
    await run_loop_iteration(app, state, job)
    assert "inspect CI" in app._stream_prompt.call_args.args[0]
    assert "existing tool permissions" in app._stream_prompt.call_args.args[0]
    path.write_text("inspect deployment", encoding="utf-8")
    await run_loop_iteration(app, state, job)
    assert "inspect deployment" in app._stream_prompt.call_args.args[0]
    assert current_loop.get() is None


@pytest.mark.asyncio
async def test_selection_race_during_instruction_read_defers_instead_of_wrong_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app, state = host(tmp_path)
    job = state._loop_scheduler.add(LoopSpec())

    def read(*_args: object) -> str:
        app.session_state = SimpleNamespace(thread_id="new")
        return "old session instruction"

    monkeypatch.setattr("novacode_cli.tui.loop_commands.load_instructions", read)
    await run_loop_iteration(app, state, job)
    app._stream_prompt.assert_not_awaited()
    assert job.due == 0


def test_poll_expiration_cancel_and_hidden_session_gate(tmp_path: Path):
    app, state = host(tmp_path)
    job = state._loop_scheduler.add(LoopSpec("watch"))
    app.session_state = SimpleNamespace()
    poll_loops(app)
    app._run_loop_job.assert_not_called()
    app.session_state = state
    poll_loops(app)
    app._run_loop_job.assert_called_once_with(state, job)
    app._active_loop_job = (state, job)
    job.expires = 0
    poll_loops(app)
    poll_loops(app)
    state.add_notification.assert_called_once()
    app.action_cancel_turn.assert_called_once()


@pytest.mark.asyncio
async def test_stop_command_and_shutdown_do_not_leave_producers(tmp_path: Path):
    app, state = host(tmp_path)
    job = state._loop_scheduler.add(LoopSpec("watch"))
    app._active_loop_job = (state, job)
    await run_loop_command(app, f"/loop stop {job.id}")
    app.action_cancel_turn.assert_called_once()
    close_loops(app)
    assert state._loop_scheduler.closed
    assert not state._loop_host


@pytest.mark.asyncio
async def test_cancellation_stops_recurrence(tmp_path: Path):
    app, state = host(tmp_path)
    job = state._loop_scheduler.add(LoopSpec("watch", interval=30))
    app._stream_prompt.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run_loop_iteration(app, state, job)
    assert not state._loop_scheduler.jobs


@pytest.mark.parametrize("reason", ["busy", "lock", "queue", "plan", "modal", "deferred"])
def test_idle_guards(tmp_path: Path, reason: str):
    app, state = host(tmp_path)
    assert loop_idle(app, state)
    if reason == "busy":
        app._turn_active = True
    elif reason == "lock":
        state._remote_message_lock = Mock(locked=Mock(return_value=True))
    elif reason == "queue":
        state._remote_message_queue = Mock(empty=Mock(return_value=False))
    elif reason == "plan":
        state.plan_mode_enabled = True
    elif reason == "modal":
        app.screen_stack = [object(), object()]
    else:
        app._deferred_prompts = ["user first"]
    assert not loop_idle(app, state)


def test_hidden_expired_loop_never_cancels_another_tabs_turn(tmp_path: Path):
    app, state = host(tmp_path)
    job = state._loop_scheduler.add(LoopSpec("watch"))
    job.expires = 0
    app._active_loop_job = (state, job)
    worker = Mock()
    app._loop_workers = {(state.thread_id, job.id): worker}
    app.session_state = SimpleNamespace(thread_id="another")
    poll_loops(app)
    app.action_cancel_turn.assert_not_called()
    worker.cancel.assert_called_once()


def test_new_conversation_discards_session_loops():
    from novacode_cli.states.Session import SessionState

    state = SessionState()
    state._loop_scheduler = LoopScheduler(state.thread_id)
    previous = state._loop_scheduler
    previous.add(LoopSpec("watch"))
    state.reset_conversation()
    assert previous.closed
    assert not previous.jobs
    assert state._loop_scheduler is None


def test_escape_while_waiting_cancels_recurrence(tmp_path: Path):
    from novacode_cli.tui.app import NovaApp

    app, state = host(tmp_path)
    state._loop_scheduler.add(LoopSpec("watch", interval=30))
    NovaApp.action_cancel_turn(app)
    assert not state._loop_scheduler.jobs
    app._log.assert_called_once()


@pytest.mark.asyncio
async def test_skills_use_agent_turn_and_direct_executables_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = AsyncMock(return_value=SimpleNamespace(prompt="review the project"))
    monkeypatch.setattr("novacode_cli.commands.skill_invoke._try_skill_invocation", resolver)
    app, state = host(tmp_path)
    job = state._loop_scheduler.add(LoopSpec("/review-pr 128"))
    await run_loop_iteration(app, state, job)
    resolver.assert_awaited_once_with("review-pr", "128", state, "agent")
    assert "review the project" in app._stream_prompt.call_args.args[0]
    app._stream_prompt.reset_mock()
    resolver.reset_mock()
    job = state._loop_scheduler.add(LoopSpec("/review-pr --run unsafe"))
    await run_loop_iteration(app, state, job)
    resolver.assert_not_awaited()
    app._stream_prompt.assert_not_awaited()
    assert not state._loop_scheduler.jobs


def test_wakeup_stays_on_direct_tool_path():
    from novacode_cli.agents.code_mode import DIRECT_ONLY_TOOLS

    assert "schedule_wakeup" in DIRECT_ONLY_TOOLS


@pytest.mark.asyncio
async def test_textual_timer_dispatches_once_and_closes_on_exit(tmp_path: Path):
    from textual import work
    from textual.app import App

    class LoopHost(App):
        def __init__(self) -> None:
            super().__init__()
            self.session_state = SimpleNamespace(
                thread_id="host",
                headless=False,
                workspace_root=str(tmp_path),
                add_notification=Mock(),
            )
            self.assistant_id = "agent"
            self._active_pane = SimpleNamespace(kind="root")
            self._log = Mock()
            self._stream_prompt = AsyncMock()

        @work(group="turn")
        async def _run_loop_job(self, state: SimpleNamespace, job: LoopJob) -> None:
            await run_loop_iteration(self, state, job)

        def on_unmount(self) -> None:
            close_loops(self)

    app = LoopHost()
    async with app.run_test() as pilot:
        await run_loop_command(app, "/loop inspect CI")
        await pilot.pause(1.3)
        await app.workers.wait_for_complete()
        app._stream_prompt.assert_awaited_once()
        assert not app.session_state._loop_scheduler.jobs
        await run_loop_command(app, "/loop 5m inspect deployment")
        assert len(app.session_state._loop_scheduler.jobs) == 1
    assert app.session_state._loop_scheduler.closed
    assert not app.session_state._loop_host
