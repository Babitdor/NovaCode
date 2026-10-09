"""Tests for the kanban board server and its watch loop.

Pin the bug fixes: state lives on the server instance (not class globals), the
"processing" queue is FIFO with nothing dropped, results are captured from the
persisted AIMessage, and failures surface on the card instead of killing the
loop.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from novacode_cli.commands.trello_handler import (
    _capture_task_result,
    _content_text,
    trello_watch_loop,
)
from novacode_cli.commands.trello_server import TrelloServer

if TYPE_CHECKING:
    import pytest


class _FakeAgent:
    """Agent stub exposing ``aget_state`` over a fixed message list."""

    def __init__(self, messages: list[object], *, boom: bool = False) -> None:
        self._messages = messages
        self._boom = boom

    async def aget_state(self, _config: object) -> SimpleNamespace:
        if self._boom:
            msg = "state exploded"
            raise RuntimeError(msg)
        return SimpleNamespace(values={"messages": self._messages})


def _ai(text: object) -> SimpleNamespace:
    return SimpleNamespace(type="ai", content=text)


# ── task lifecycle ──────────────────────────────────────────────────────────


def test_add_task_defaults_to_loaded():
    server = TrelloServer()
    task = server.add_task("build the thing")
    assert task["status"] == "loaded"
    assert task["started_at"] is None
    assert task["completed_at"] is None
    assert task["result"] is None


def test_move_sets_and_clears_timestamps():
    server = TrelloServer()
    task = server.add_task("x")
    processing = server.move_task(task["id"], "processing")
    assert processing["started_at"] is not None
    assert processing["completed_at"] is None
    done = server.move_task(task["id"], "done")
    assert done["completed_at"] is not None
    # Reopening must clear the completed timestamp again.
    reopened = server.move_task(task["id"], "processing")
    assert reopened["completed_at"] is None


def test_move_invalid_status_returns_none():
    server = TrelloServer()
    task = server.add_task("x")
    assert server.move_task(task["id"], "bogus") is None


def test_move_missing_task_returns_none():
    assert TrelloServer().move_task("nope", "done") is None


def test_delete_removes_task():
    server = TrelloServer()
    task = server.add_task("x")
    assert server.delete_task(task["id"])["id"] == task["id"]
    assert server.get_tasks() == []
    assert server.delete_task(task["id"]) is None


# ── the dropped-notification regression ─────────────────────────────────────


def test_next_processing_is_fifo_and_drops_nothing():
    server = TrelloServer()
    a = server.add_task("a")
    b = server.add_task("b")
    # Two cards moved to processing in quick succession — the old single-slot
    # mechanism lost one of these. FIFO must serve both, oldest first.
    server.move_task(a["id"], "processing")
    server.move_task(b["id"], "processing")
    assert server.next_processing_task()["id"] == a["id"]
    server.mark_done(a["id"])
    assert server.next_processing_task()["id"] == b["id"]
    server.mark_done(b["id"])
    assert server.next_processing_task() is None


def test_pop_next_loaded_advances_oldest():
    server = TrelloServer()
    a = server.add_task("a")
    server.add_task("b")
    popped = server.pop_next_loaded_task()
    assert popped["id"] == a["id"]
    assert popped["status"] == "processing"


def test_mark_done_attaches_result():
    server = TrelloServer()
    task = server.add_task("x")
    server.move_task(task["id"], "processing")
    server.mark_done(task["id"], "the output")
    done = server.get_tasks()[0]
    assert done["status"] == "done"
    assert done["result"] == "the output"
    assert done["completed_at"] is not None


# ── state / settings ────────────────────────────────────────────────────────


def test_get_state_shape():
    server = TrelloServer()
    server.add_task("x")
    server.set_auto_advance(True)
    server.set_running("rid")
    state = server.get_state()
    assert set(state) == {"tasks", "auto_advance", "running_id", "remote"}
    assert state["auto_advance"] is True
    assert state["running_id"] == "rid"
    assert len(state["tasks"]) == 1


def test_task_counts():
    server = TrelloServer()
    server.add_task("a")
    b = server.add_task("b")
    c = server.add_task("c")
    server.move_task(b["id"], "processing")
    server.move_task(c["id"], "done")
    assert server.get_task_counts() == {"loaded": 1, "processing": 1, "done": 1}


# ── result capture helper ───────────────────────────────────────────────────


def test_content_text_variants():
    assert _content_text("hi") == "hi"
    assert _content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert _content_text([{"type": "tool_use", "name": "x"}]) == ""
    assert _content_text("") == ""
    assert _content_text(None) == ""


async def test_capture_result_reads_last_ai_message(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.core.input_preparation as ip

    monkeypatch.setattr(ip, "build_agent_config", lambda *_a, **_k: {})
    agent = _FakeAgent([SimpleNamespace(type="human", content="do x"), _ai("done x")])
    out = await _capture_task_result(agent, "nova-agent", SimpleNamespace(thread_id="t1"))
    assert out == "done x"


async def test_capture_result_swallows_errors(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.core.input_preparation as ip

    monkeypatch.setattr(ip, "build_agent_config", lambda *_a, **_k: {})
    agent = _FakeAgent([], boom=True)
    out = await _capture_task_result(agent, "nova-agent", SimpleNamespace(thread_id="t1"))
    assert out is None


# ── watch loop ──────────────────────────────────────────────────────────────


async def test_watch_loop_processes_and_captures(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.core.input_preparation as ip

    monkeypatch.setattr(ip, "build_agent_config", lambda *_a, **_k: {})
    server = TrelloServer()
    server.is_running = True
    task = server.add_task("build the thing")
    server.move_task(task["id"], "processing")

    running_seen: list[str | None] = []

    async def fake_execute(
        _desc: object, _ag: object, _aid: object, _sess: object, _tok: object
    ) -> None:
        running_seen.append(server.running_id)  # set to the task id mid-run
        server.is_running = False  # end the loop after this one task

    await trello_watch_loop(
        server,
        _FakeAgent([_ai("all done")]),
        "nova-agent",
        SimpleNamespace(thread_id="t1"),
        None,
        fake_execute,
        lambda *_a, **_k: None,
    )

    done = server.get_tasks()[0]
    assert done["status"] == "done"
    assert done["result"] == "all done"
    assert running_seen == [task["id"]]
    assert server.running_id is None


async def test_watch_loop_records_failure(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.core.input_preparation as ip

    monkeypatch.setattr(ip, "build_agent_config", lambda *_a, **_k: {})
    server = TrelloServer()
    server.is_running = True
    task = server.add_task("x")
    server.move_task(task["id"], "processing")

    async def boom(_desc: object, _ag: object, _aid: object, _sess: object, _tok: object) -> None:
        server.is_running = False
        msg = "kaboom"
        raise RuntimeError(msg)

    await trello_watch_loop(
        server,
        _FakeAgent([]),
        None,
        SimpleNamespace(thread_id="t"),
        None,
        boom,
        lambda *_a, **_k: None,
    )

    done = server.get_tasks()[0]
    assert done["status"] == "done"
    assert "kaboom" in (done["result"] or "")
    assert server.running_id is None


async def test_watch_loop_respects_auto_advance(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.core.input_preparation as ip

    monkeypatch.setattr(ip, "build_agent_config", lambda *_a, **_k: {})
    server = TrelloServer()
    server.is_running = True
    # A loaded card with auto-advance OFF must not be picked.
    server.add_task("waiting")

    calls: list[str] = []

    async def fake_execute(
        desc: str, _ag: object, _aid: object, _sess: object, _tok: object
    ) -> None:
        calls.append(desc)
        server.is_running = False

    # auto_advance is False by default → loop should idle. Flip it on, then the
    # loaded card gets pulled into processing and run.
    server.set_auto_advance(True)
    await trello_watch_loop(
        server,
        _FakeAgent([_ai("ok")]),
        None,
        SimpleNamespace(thread_id="t"),
        None,
        fake_execute,
        lambda *_a, **_k: None,
    )
    assert calls == ["waiting"]
    assert server.get_tasks()[0]["status"] == "done"
