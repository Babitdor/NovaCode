"""The child half of a parallel session: stdio JSONL, forwarded HITL, one turn.

``SessionWorker`` is the interactive sibling of the headless runner. The contract
these tests pin:

* it announces itself with ``ready`` before anything else;
* every agent event reaches the parent as an ``ev`` frame;
* a prompt arriving mid-turn is **queued and later run**, never dropped — and it
  starts as soon as the turn ends, even if the parent sends nothing further;
* interrupts are forwarded to the parent and **always** resolved: a silent or
  departed parent fails closed, never wedging the graph and never auto-approving;
* ``cancel`` ends the active turn, ``shutdown`` exits cleanly.

The worker is driven in-process against a fake event stream by seeding its
``inbox``, so nothing here needs a model, an API key, or a subprocess.

Runnable directly (``python tests/test_session_worker.py``) or via pytest.
"""

from __future__ import annotations

import asyncio

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.sessions import protocol
from novacode_cli.sessions.worker import SessionWorker


class _SS:
    """Minimal session-state stand-in (mirrors tests/test_tui_app.py's _SS)."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos = None
        self.steering_instructions = []
        self.headless_out_fd = None


def _make(monkeypatch, stream):
    """A worker whose turns yield *stream*, capturing emitted frames.

    *stream* is an async-generator function ``(text, *a, **kw)`` or a plain list
    of events to replay for every prompt.
    """
    if not callable(stream):
        events = list(stream)

        async def gen(text, *a, **kw):
            for e in events:
                yield e

        stream = gen

    import novacode_cli.sessions.worker as mod

    monkeypatch.setattr(mod, "iterate_agent_events", stream)

    w = SessionWorker(agent=object(), assistant_id="nova-agent", session_state=_SS())
    sent: list[dict] = []
    w._emit = sent.append  # capture instead of writing to a pipe
    # The stdin pump would block on a real terminal; the inbox is seeded directly.
    w._stdin_pump = _never  # type: ignore[assignment]
    return w, sent


async def _never() -> None:
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_job_controls_use_worker_registry_and_ignore_output_events(monkeypatch):
    from novacode_cli.shell import jobs
    reg = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: reg)
    worker, sent = _make(monkeypatch, [])
    running = asyncio.create_task(worker.run())
    assert await _wait_for(lambda: bool(_frames(sent, "ready")))
    job = reg.add("worker command", "shell")
    frames = len(_frames(sent, "jobs"))
    reg.append_log(job.id, "output")
    assert len(_frames(sent, "jobs")) == frames
    await worker.inbox.put({"t": "job_control", "action": "terminate", "job_id": job.task_id})
    assert await _wait_for(job.kill.is_set)
    assert _frames(sent, "jobs")[-1]["jobs"][0]["logs"] == []
    await worker.inbox.put({"t": "job_control", "action": "logs", "job_id": job.task_id, "request_id": "log-1"})
    assert await _wait_for(lambda: bool(_frames(sent, "job_logs")))
    assert _frames(sent, "job_logs")[-1] == {"t": "job_logs", "request_id": "log-1", "output": "output"}
    await worker.inbox.put({"t": "shutdown"})
    await running
    assert not reg._observers


@pytest.mark.asyncio
async def test_shutdown_stops_worker_background_process(monkeypatch):
    import sys
    from novacode_cli.shell import jobs
    from novacode_cli.shell.middleware import ShellMiddleware

    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    worker, sent = _make(monkeypatch, [])
    running = asyncio.create_task(worker.run())
    assert await _wait_for(lambda: bool(_frames(sent, "ready")))
    command = "import time;time.sleep(60)"
    job = registry.add(command, "shell", [sys.executable, "-c"])
    middleware = ShellMiddleware(workspace_root=".")
    background = asyncio.create_task(middleware._bg_run(command, job.prog, job.id))
    try:
        assert await _wait_for(lambda: job.pid is not None)
        await worker.inbox.put({"t": "shutdown"})
        await asyncio.wait_for(running, 10)
        await asyncio.wait_for(background, 5)
        assert job.status == "terminated"
        assert not registry._observers
    finally:
        job.kill.set()
        if not background.done():
            await asyncio.wait_for(background, 10)
        if not running.done():
            await worker.inbox.put({"t": "shutdown"})
            await asyncio.wait_for(running, 10)


def _frames(sent, kind):
    return [m for m in sent if m.get("t") == kind]


def _events(sent):
    return [protocol.decode_event(m) for m in sent if m.get("t") == "ev"]


async def _wait_for(pred, *, timeout=5.0):
    """Poll until *pred* is true; the worker runs concurrently in a task."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def _run_prompts(worker, sent, prompts, *, timeout=15.0):
    """Send *prompts*, wait for each turn_done, then shut down cleanly.

    Mirrors a real parent: it does not close the session out from under a turn.
    """
    task = asyncio.create_task(worker.run())
    for pid, text in prompts:
        worker.inbox.put_nowait({"t": "prompt", "id": pid, "text": text})
    ok = await _wait_for(
        lambda: {f["id"] for f in _frames(sent, "turn_done")} >= {p for p, _ in prompts},
        timeout=timeout,
    )
    worker.inbox.put_nowait({"t": "shutdown"})
    code = await asyncio.wait_for(task, timeout=timeout)
    assert ok, f"not all turns finished: {_frames(sent, 'turn_done')}"
    return code


# ── lifecycle ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_approval_mode_cannot_escalate_active_turn(monkeypatch):
    seen = []
    active = asyncio.Event()
    release = asyncio.Event()

    async def stream(text, agent, aid, state, **kwargs):
        seen.append((text, state.auto_approve))
        if text == "manual":
            active.set()
            await release.wait()
            assert state.auto_approve is False
        yield ev.Done()

    worker, sent = _make(monkeypatch, stream)
    running = asyncio.create_task(worker.run())
    try:
        await worker.inbox.put({"t": "prompt", "id": "p1", "text": "manual", "auto_approve": False})
        await asyncio.wait_for(active.wait(), 5)
        await worker.inbox.put({"t": "prompt", "id": "p2", "text": "auto", "auto_approve": True})
        await asyncio.sleep(0.05)
        assert worker.session_state.auto_approve is False
        release.set()
        assert await _wait_for(lambda: len(_frames(sent, "turn_done")) == 2)
        assert seen == [("manual", False), ("auto", True)]
    finally:
        release.set()
        worker.inbox.put_nowait({"t": "shutdown"})
        await asyncio.wait_for(running, 5)


@pytest.mark.asyncio
async def test_image_prompt_survives_protocol_and_stays_in_its_session(monkeypatch):
    from novacode_cli.core.input_preparation import prepare_input_content
    from novacode_cli.image_utils import ImageData
    from novacode_cli.sessions.supervisor import SessionSupervisor

    monkeypatch.setattr("novacode_cli.core.input_preparation._main_model_can_see_images", lambda: True)
    captured = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def stream(text, *args, image_tracker=None, **kwargs):
        captured.append(await prepare_input_content(text, image_tracker, skip_file_mentions=True))
        if text == "busy":
            started.set()
            await release.wait()
        yield ev.Done()

    worker, sent = _make(monkeypatch, stream)
    supervisor = SessionSupervisor.__new__(SessionSupervisor)
    supervisor._children = {"child": type("Child", (), {})()}
    import itertools
    supervisor._prompt_ids = itertools.count(1)

    async def send(child, payload):
        worker.inbox.put_nowait(protocol.loads(protocol.dumps(payload)))
        return True

    supervisor._send = send
    running = asyncio.create_task(worker.run())
    worker.inbox.put_nowait({"t": "prompt", "id": "busy", "text": "busy"})
    await asyncio.wait_for(started.wait(), 5)
    image = ImageData("a" * 300_000, "png", "[image]")
    await supervisor.send_prompt("child", "inspect", images=[image])
    await supervisor.send_prompt("child", "follow up")
    await asyncio.sleep(0.05)
    assert worker.image_tracker is None, "queued image must not change the current turn"
    release.set()
    assert await _wait_for(lambda: len(_frames(sent, "turn_done")) == 3)
    worker.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(running, 5)
    assert captured[0] == "busy"
    assert captured[1][0]["text"] == "inspect"
    assert captured[1][1]["image_url"]["url"] == image.to_data_url()
    assert captured[2][1] == captured[1][1], "follow-up retains the image"


@pytest.mark.asyncio
async def test_invalid_image_frame_does_not_stop_session(monkeypatch):
    worker, sent = _make(monkeypatch, [ev.Done()])
    running = asyncio.create_task(worker.run())
    worker.inbox.put_nowait({"t": "prompt", "id": "bad", "text": "hi", "images": [None]})
    worker.inbox.put_nowait({"t": "prompt", "id": "good", "text": "hello"})
    assert await _wait_for(lambda: len(_frames(sent, "turn_done")) == 2)
    worker.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(running, 5)
    assert _frames(sent, "turn_done")[0]["ok"] is False
    assert _frames(sent, "turn_done")[1]["ok"] is True


@pytest.mark.timeout(30)
async def test_announces_ready_first(monkeypatch):
    w, sent = _make(monkeypatch, [ev.Done()])
    task = asyncio.create_task(w.run())
    await _wait_for(lambda: sent)
    w.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(task, timeout=10)

    assert sent[0]["t"] == "ready"
    assert sent[0]["session_id"] == "s1"
    assert sent[0]["thread_id"] == "t1"
    assert "cwd" in sent[0]


@pytest.mark.timeout(30)
async def test_shutdown_returns_ok(monkeypatch):
    w, sent = _make(monkeypatch, [ev.Done()])
    task = asyncio.create_task(w.run())
    w.inbox.put_nowait({"t": "shutdown"})
    assert await asyncio.wait_for(task, timeout=10) == 0


@pytest.mark.timeout(30)
async def test_eof_on_stdin_ends_the_worker(monkeypatch):
    # The parent dying closes our stdin; that must exit, not hang.
    w, sent = _make(monkeypatch, [ev.Done()])
    task = asyncio.create_task(w.run())
    w.inbox.put_nowait(None)
    assert await asyncio.wait_for(task, timeout=10) == 0


@pytest.mark.timeout(30)
async def test_prompt_streams_events_then_turn_done(monkeypatch):
    w, sent = _make(
        monkeypatch,
        [
            ev.StatusUpdate(message="thinking"),
            ev.AssistantMessage(text="hi", agent_name="nova", agent_color="cyan"),
            ev.Done(had_response=True),
        ],
    )
    await _run_prompts(w, sent, [("p1", "hello")])

    got = _events(sent)
    assert any(isinstance(e, ev.AssistantMessage) and e.text == "hi" for e in got)
    assert any(isinstance(e, ev.Done) for e in got)
    assert _frames(sent, "turn_done")[0] == {"t": "turn_done", "id": "p1", "ok": True}


@pytest.mark.timeout(30)
async def test_blank_prompt_completes_without_a_turn(monkeypatch):
    w, sent = _make(monkeypatch, [ev.Done()])
    await _run_prompts(w, sent, [("p1", "   ")])

    assert _frames(sent, "turn_done")[0]["id"] == "p1"
    assert not _events(sent)  # nothing was streamed


@pytest.mark.timeout(30)
async def test_error_event_marks_turn_not_ok(monkeypatch):
    w, sent = _make(monkeypatch, [ev.Error(message="boom"), ev.Done()])
    await _run_prompts(w, sent, [("p1", "x")])

    assert _frames(sent, "turn_done")[0]["ok"] is False


@pytest.mark.timeout(30)
async def test_agent_exception_is_reported_not_fatal(monkeypatch):
    async def boom(text, *a, **kw):
        raise RuntimeError("agent exploded")
        yield  # pragma: no cover - makes this an async generator

    w, sent = _make(monkeypatch, boom)
    assert await _run_prompts(w, sent, [("p1", "x")]) == 0

    errs = [e for e in _events(sent) if isinstance(e, ev.Error)]
    assert errs and "agent exploded" in errs[0].message
    assert _frames(sent, "turn_done")[0]["ok"] is False


# ── queueing: a prompt mid-turn must not be dropped or starved ───────────────


@pytest.mark.timeout(30)
async def test_prompt_during_turn_is_queued_then_run(monkeypatch):
    seen: list[str] = []
    gate = asyncio.Event()

    async def slow(text, *a, **kw):
        seen.append(text)
        if text == "first":
            await gate.wait()  # hold the first turn open
        yield ev.Done()

    w, sent = _make(monkeypatch, slow)
    task = asyncio.create_task(w.run())

    w.inbox.put_nowait({"t": "prompt", "id": "p1", "text": "first"})
    await _wait_for(lambda: seen == ["first"])
    w.inbox.put_nowait({"t": "prompt", "id": "p2", "text": "second"})
    await _wait_for(lambda: bool(w._queued))
    assert w._queued, "second prompt should be waiting, not dropped"

    # Release the first turn and send NOTHING else: the queued prompt must start
    # on turn completion alone, or it would starve until the next parent message.
    gate.set()
    assert await _wait_for(lambda: seen == ["first", "second"], timeout=5)

    w.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(task, timeout=10)
    assert {f["id"] for f in _frames(sent, "turn_done")} == {"p1", "p2"}


@pytest.mark.timeout(30)
async def test_cancel_drops_queued_prompts(monkeypatch):
    started = asyncio.Event()

    async def forever(text, *a, **kw):
        started.set()
        await asyncio.sleep(3600)
        yield ev.Done()  # pragma: no cover

    w, sent = _make(monkeypatch, forever)
    task = asyncio.create_task(w.run())

    w.inbox.put_nowait({"t": "prompt", "id": "p1", "text": "x"})
    await asyncio.wait_for(started.wait(), timeout=5)
    w.inbox.put_nowait({"t": "prompt", "id": "p2", "text": "y"})
    await _wait_for(lambda: bool(w._queued))

    w.inbox.put_nowait({"t": "cancel"})
    assert await _wait_for(lambda: not w._queued and not w._turn_running())

    w.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(task, timeout=10)


# ── HITL forwarding ──────────────────────────────────────────────────────────


def _interrupt_stream(box):
    async def gen(text, *a, **kw):
        req = ev.InterruptRequest(
            kind="tool", payload={"action_requests": [{"n": 1}]}, future=None
        )
        req.future = asyncio.get_running_loop().create_future()
        box.append(req)
        yield req
        await req.future  # the graph waits on the decision, like the real loop
        yield ev.Done()

    return gen


@pytest.mark.timeout(30)
async def test_interrupt_is_forwarded_and_reply_resolves_it(monkeypatch):
    box: list = []
    w, sent = _make(monkeypatch, _interrupt_stream(box))
    task = asyncio.create_task(w.run())

    w.inbox.put_nowait({"t": "prompt", "id": "p1", "text": "x"})
    assert await _wait_for(lambda: _frames(sent, "interrupt"))

    ask = _frames(sent, "interrupt")[0]
    assert ask["kind"] == "tool"
    assert ask["payload"] == {"action_requests": [{"n": 1}]}

    decision = {"decisions": [{"type": "approve"}], "any_rejected": False}
    w.inbox.put_nowait({"t": "interrupt_reply", "id": ask["id"], "result": decision})

    assert await _wait_for(lambda: box[0].future.done())
    assert box[0].future.result() == decision

    w.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(task, timeout=10)


@pytest.mark.timeout(30)
async def test_shutdown_while_awaiting_approval_fails_closed(monkeypatch):
    """No answer must resolve to the safe default — never hang, never approve."""
    box: list = []
    w, sent = _make(monkeypatch, _interrupt_stream(box))
    task = asyncio.create_task(w.run())

    w.inbox.put_nowait({"t": "prompt", "id": "p1", "text": "x"})
    assert await _wait_for(lambda: _frames(sent, "interrupt"))

    w.inbox.put_nowait({"t": "shutdown"})  # parent leaves without deciding
    await asyncio.wait_for(task, timeout=10)

    req = box[0]
    assert req.future.done(), "interrupt must never be left dangling"
    assert req.future.result() != {"decisions": [{"type": "approve"}], "any_rejected": False}


@pytest.mark.timeout(30)
async def test_unknown_interrupt_reply_id_is_ignored(monkeypatch):
    box: list = []
    w, sent = _make(monkeypatch, _interrupt_stream(box))
    task = asyncio.create_task(w.run())

    w.inbox.put_nowait({"t": "prompt", "id": "p1", "text": "x"})
    assert await _wait_for(lambda: _frames(sent, "interrupt"))

    w.inbox.put_nowait({"t": "interrupt_reply", "id": "bogus", "result": {"x": 1}})
    await asyncio.sleep(0.1)
    assert not box[0].future.done()  # the real one is still waiting

    w.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(task, timeout=10)


# ── cancel ───────────────────────────────────────────────────────────────────


@pytest.mark.timeout(30)
async def test_cancel_stops_the_active_turn(monkeypatch):
    started = asyncio.Event()

    async def forever(text, *a, **kw):
        started.set()
        await asyncio.sleep(3600)
        yield ev.Done()  # pragma: no cover

    w, sent = _make(monkeypatch, forever)
    task = asyncio.create_task(w.run())

    w.inbox.put_nowait({"t": "prompt", "id": "p1", "text": "x"})
    await asyncio.wait_for(started.wait(), timeout=5)

    w.inbox.put_nowait({"t": "cancel"})
    assert await _wait_for(lambda: not w._turn_running())

    w.inbox.put_nowait({"t": "shutdown"})
    await asyncio.wait_for(task, timeout=10)
    assert any(isinstance(e, ev.Cancelled) for e in _events(sent))


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))
