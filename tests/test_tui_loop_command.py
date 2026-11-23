"""`/loop` in the socket port: a bounded recurrence the server owns.

The terminal UI streams each iteration into the transcript. A socket port cannot
promise a listener exists when the timer fires, so an iteration runs through the
headless executor and its progress arrives as blocks. These tests pin the
scheduling rules that matter: one claim per tick, no run while busy, expiry,
and the instruction the model receives.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from novacode_cli.loops import LoopScheduler, LoopSpec
from novacode_cli.server.catalog import CATALOG
from novacode_cli.ui.port_server import ServerPort


def _state(thread_id: str = "t1") -> SimpleNamespace:
    """A session state with somewhere to put a scheduler."""
    return SimpleNamespace(
        thread_id=thread_id,
        session_id="s1",
        workspace_root="",
        headless=False,
        _loop_scheduler=None,
    )


def _port(
    state: SimpleNamespace | None = None, *, sent: list[dict[str, Any]] | None = None
) -> ServerPort:
    """A port over one session."""
    worker = SimpleNamespace(
        agent=object(),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
        token_tracker=None,
        session_state=state if state is not None else _state(),
    )
    sink: list[dict[str, Any]] = [] if sent is None else sent
    return ServerPort(sink.append, worker)


async def test_an_interval_loop_is_registered_with_the_shared_scheduler() -> None:
    state = _state()
    port = _port(state)

    result = await port._loop_command("1m check the build")

    assert result.ok is True
    assert "Loop" in result.message
    scheduler = state._loop_scheduler
    assert scheduler is not None
    assert len(scheduler.jobs) == 1
    job = next(iter(scheduler.jobs.values()))
    assert job.spec.interval == 60
    assert job.spec.prompt == "check the build"
    assert port._loop_poll is not None
    port._loop_poll.cancel()


async def test_the_listing_stays_in_the_transcript_as_blocks() -> None:
    state = _state()
    port = _port(state)
    await port._loop_command("1m check the build")

    result = await port._loop_command("list")

    assert result.ok is True
    assert result.message == ""
    assert len(result.blocks) == 1
    assert "check the build" in result.blocks[0].text
    port._loop_poll.cancel()


async def test_stop_clears_the_session_loops() -> None:
    state = _state()
    port = _port(state)
    await port._loop_command("1m check the build")

    result = await port._loop_command("stop")

    assert result.message == "Stopped 1 loop(s)."
    assert not state._loop_scheduler.jobs
    port._loop_poll.cancel()


async def test_a_bad_schedule_reports_the_scheduler_wording() -> None:
    port = _port()

    result = await port._loop_command("5x please")

    assert result.ok is False
    assert result.message.startswith("/loop: ")


async def test_a_port_without_a_conversation_refuses() -> None:
    port = _port(SimpleNamespace())

    result = await port._loop_command("1m check the build")

    assert result.ok is False
    assert "live conversation" in result.message


async def test_a_due_job_runs_one_headless_iteration(monkeypatch: Any) -> None:  # noqa: ANN401
    state = _state()
    scheduler = LoopScheduler("t1")
    job = scheduler.add(LoopSpec(prompt="check the build", interval=60, ttl=600))
    job.due = 0
    state._loop_scheduler = scheduler
    calls: list[dict[str, Any]] = []

    async def execute_task(user_input: str, *_args: Any, **_kwargs: Any) -> None:
        calls.append({"input": user_input})

    monkeypatch.setattr("novacode_cli.ui.execution.execute_task", execute_task)
    port = _port(state, sent=[])
    port._loop_states.append(state)

    port._tick_loops()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert calls[0]["input"].startswith(f"[Session loop {job.id}]")
    assert "check the build" in calls[0]["input"]
    assert job.iterations == 1
    assert job.id in scheduler.jobs  # an interval keeps its own deadline
    assert port._loop_jobs == {}


async def test_a_busy_port_keeps_the_job_waiting() -> None:
    state = _state()
    scheduler = LoopScheduler("t1")
    job = scheduler.add(LoopSpec(prompt="check the build", interval=60, ttl=600))
    job.due = 0
    state._loop_scheduler = scheduler
    worker_port = _port(state)
    worker_port._busy = lambda: True
    worker_port._loop_states.append(state)

    worker_port._tick_loops()

    assert job.running is False
    assert job.due == 0


async def test_an_expired_job_is_dropped_and_announced() -> None:
    state = _state()
    scheduler = LoopScheduler("t1")
    job = scheduler.add(LoopSpec(prompt="check the build", interval=60, ttl=600))
    job.expires = 0
    state._loop_scheduler = scheduler
    sent: list[dict[str, Any]] = []
    port = _port(state, sent=sent)
    port._loop_states.append(state)

    port._tick_loops()

    assert not scheduler.jobs
    assert sent[0]["blocks"][0]["text"] == f"Loop {job.id} expired."


def test_the_catalogue_serves_the_loop() -> None:
    assert CATALOG["loop"].served is True
