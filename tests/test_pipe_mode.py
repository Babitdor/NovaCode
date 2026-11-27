"""Persistent public pipe protocol, using fake agents and real Windows pipes."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.headless.pipe import PipeSession, run_pipe_bootstrap


def make(monkeypatch, stream=None):
    async def events(text, *args, **kwargs):
        yield ev.AssistantMessage(text, "Nova", "blue")
        yield ev.Done(True)

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream or events)
    state = SimpleNamespace(
        session_id="s1",
        thread_id="t1",
        auto_approve=False,
        plan_mode_enabled=False,
        headless_out_fd=None,
        headless_exit_code=0,
        pipe_startup_timeout=0.05,
        steering_instructions=[],
    )
    worker = PipeSession(agent=object(), assistant_id="nova", session_state=state)
    records = []
    worker._output._writeln = records.append

    async def never():
        await asyncio.Future()

    async def save(*, is_error=False):
        pass

    worker._stdin_pump = never
    worker._save = save
    return worker, records


async def wait_for(check):
    async with asyncio.timeout(3):
        while not check():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_multiple_requests_and_replay(monkeypatch):
    worker, records = make(monkeypatch)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: records)
    assert records[0]["type"] == "ready"
    for pid in ("one", "two"):
        await worker.inbox.put({"type": "prompt", "id": pid, "content": pid})
    await wait_for(lambda: len([r for r in records if r["type"] == "done"]) == 2)
    assert [r["content"] for r in records if r["type"] == "text"] == ["one", "two"]
    assert all(r["ok"] for r in records if r["type"] == "done")
    await worker.inbox.put({"type": "prompt", "id": "one", "content": "one"})
    await wait_for(lambda: any(r.get("replayed") for r in records))
    await worker.inbox.put({"type": "shutdown"})
    assert await task == 0
    assert len([r for r in records if r["type"] == "started"]) == 2
    assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))


@pytest.mark.parametrize(
    "message,code",
    [
        ([], "invalid_input"),
        ({"type": "prompt", "content": "hi"}, "invalid_request"),
        ({"type": "prompt", "id": "x", "content": "\ud800"}, "invalid_request"),
        ({"type": "prompt", "id": "x", "content": "hi", "auto_approve": True}, "invalid_request"),
        (
            {"type": "approval", "interrupt_id": "previous-process:i1", "decision": "approve"},
            "stale_interrupt",
        ),
        ({"type": "oops"}, "unknown_command"),
    ],
)
def test_validation(monkeypatch, message, code):
    worker, records = make(monkeypatch)
    assert worker.translate(message) == {}
    assert records[-1]["code"] == code
    assert not worker.session_state.auto_approve


def test_conflict_capacity_and_cancel(monkeypatch):
    worker, records = make(monkeypatch)
    assert worker.translate({"type": "prompt", "id": "x", "content": "hi"})["t"] == "prompt"
    worker.translate({"type": "prompt", "id": "x", "content": "different"})
    assert records[-1]["code"] == "request_id_conflict"
    for i in range(worker.MAX_PENDING):
        worker._queued.append((f"q{i}", "queued", [], False))
    worker.translate({"type": "prompt", "id": "overflow", "content": "hi"})
    assert records[-1]["code"] == "queue_full"
    worker.translate({"type": "cancel"})
    assert not worker._queued
    assert len([r for r in records if r["type"] == "done"]) == 16


def test_nonfinite_metadata_is_valid_json(monkeypatch):
    worker, records = make(monkeypatch)
    worker.publish("usage", usage={"cost": float("nan"), "other": [float("inf")]})
    assert records[-1]["usage"] == {"cost": None, "other": [None]}
    json.dumps(records[-1], allow_nan=False)


def test_ready_advertises_command_and_images(monkeypatch):
    worker, records = make(monkeypatch)
    worker._frame({"t": "ready"})
    capabilities = records[0]["capabilities"]
    assert "command" in capabilities
    assert "images" in capabilities
    assert "steer" in capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [True, False])
async def test_desktop_guidance_stays_in_the_target_turn(monkeypatch, consume):
    from novacode_cli.bootstrap.steering import SteeringMiddleware

    resume = asyncio.Event()

    async def stream(text, *args, **kwargs):
        await resume.wait()
        update = middleware.before_model({}, None) if consume else None
        if update:
            yield ev.AssistantMessage(update["messages"][0].content, "Nova", "blue")
        yield ev.Done(True)

    worker, records = make(monkeypatch, stream)
    instructions = worker.session_state.steering_instructions
    middleware = SteeringMiddleware(instructions=instructions)
    task = asyncio.create_task(worker.run())
    try:
        await worker.inbox.put({"type": "prompt", "id": "running", "content": "initial"})
        await wait_for(lambda: any(record["type"] == "started" for record in records))
        for target in ("previous", "running"):
            await worker.inbox.put(
                {"type": "steer", "id": target, "request_id": target, "content": "/literal"}
            )
        await wait_for(lambda: sum(record["type"] == "steer_result" for record in records) == 2)
        results = [record for record in records if record["type"] == "steer_result"]
        assert [(record["id"], record["ok"]) for record in results] == [
            ("previous", False),
            ("running", True),
        ]
        resume.set()
        await wait_for(lambda: any(record["type"] == "done" for record in records))
        assert [record["request_id"] for record in records if record["type"] == "started"] == [
            "running"
        ]
        text = [record["content"] for record in records if record["type"] == "text"]
        missed = [record["count"] for record in records if record["type"] == "steer_undelivered"]
        if consume:
            assert text == [
                "[Guidance from a desktop tool. Follow the usual user approval process.]\n/literal"
            ]
            assert not missed
        else:
            assert not text
            assert missed == [1]
        assert not instructions
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_command_frame_runs_without_a_turn(monkeypatch):
    worker, records = make(monkeypatch)

    async def dispatch(text):
        return "effort is high", None, False

    monkeypatch.setattr(worker, "_dispatch_slash", dispatch)
    assert worker.translate({"type": "command", "id": "c1", "content": "/effort high"}) == {
        "t": "command",
        "id": "c1",
        "text": "/effort high",
    }


@pytest.mark.asyncio
async def test_command_frame_result(monkeypatch):
    worker, records = make(monkeypatch)

    async def dispatch(text):
        return "effort is high", None, False

    monkeypatch.setattr(worker, "_dispatch_slash", dispatch)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: records)
    await worker.inbox.put({"type": "command", "id": "c1", "content": "/effort high"})
    await wait_for(lambda: any(r["type"] == "command_result" for r in records))
    result = next(r for r in records if r["type"] == "command_result")
    assert result["request_id"] == "c1"
    assert result["ok"] is True
    assert result["output"] == "effort is high"
    # Commands never create agent turns.
    assert not any(r["type"] == "started" for r in records)
    await worker.inbox.put({"type": "shutdown"})
    assert await task == 0


@pytest.mark.asyncio
async def test_command_frame_returns_kickoff_without_starting_a_turn(monkeypatch):
    worker, records = make(monkeypatch)

    async def dispatch(text):
        return "Goal set.", "do the thing", False

    monkeypatch.setattr(worker, "_dispatch_slash", dispatch)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: records)
    await worker.inbox.put({"type": "command", "id": "g1", "content": "/goal do the thing"})
    await wait_for(lambda: any(r["type"] == "command_result" for r in records))
    result = next(r for r in records if r["type"] == "command_result")
    assert result["ok"] is True
    assert result["kickoff"] == "do the thing"
    # The controller decides whether to run the kickoff: no auto-start.
    assert not any(r["type"] == "started" for r in records)
    await worker.inbox.put({"type": "shutdown"})
    assert await task == 0


def test_command_frame_rejected_while_turn_running(monkeypatch):
    worker, records = make(monkeypatch)

    async def hang():
        await asyncio.Future()

    worker._turn = asyncio.get_event_loop().create_task(hang()) if False else None

    # _turn_running checks is_done(); a finished task is "not running", so
    # simulate a live one with an object.
    class _Live:
        def done(self):
            return False

    worker._turn = _Live()
    worker.translate({"type": "command", "id": "c", "content": "/effort"})
    assert records[-1]["code"] == "turn_running"


def test_command_frame_validation(monkeypatch):
    worker, records = make(monkeypatch)
    worker.translate({"type": "command", "content": "/effort"})
    assert records[-1]["code"] == "invalid_request"
    worker.translate({"type": "command", "id": "c", "content": "   "})
    assert records[-1]["code"] == "invalid_request"


def test_images_are_validated_and_passed_through(monkeypatch):
    worker, records = make(monkeypatch)
    frame = {
        "type": "prompt",
        "id": "img",
        "content": "what is this",
        "images": [{"base64_data": "aGVsbG8=", "format": "png"}],
    }
    translated = worker.translate(frame)
    assert translated["images"] == [
        {"base64_data": "aGVsbG8=", "format": "png", "placeholder": "[image 1]"}
    ]
    # Errors:
    worker.translate(
        {"type": "prompt", "id": "img2", "content": "x", "images": [{"base64_data": "aA=="}]}
    )
    assert records[-1]["code"] == "invalid_request"
    worker.translate(
        {
            "type": "prompt",
            "id": "img3",
            "content": "x",
            "images": [{"base64_data": "aA==", "format": "tiff"}],
        }
    )
    assert records[-1]["code"] == "invalid_request"
    worker.translate(
        {
            "type": "prompt",
            "id": "img4",
            "content": "x",
            "images": [{"base64_data": "aA==", "format": "png"} for _ in range(6)],
        }
    )
    assert records[-1]["code"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject", "timeout"])
async def test_human_approval_and_expiry(monkeypatch, decision):
    answers = []

    async def events(*args, **kwargs):
        future = asyncio.get_running_loop().create_future()
        yield ev.InterruptRequest("tool", {"action_requests": [{"name": "write_file"}]}, future)
        answers.append(await future)
        yield ev.Done(True)

    monkeypatch.setattr("novacode_cli.headless.pipe.evaluate_tool_actions", lambda *a, **k: [None])
    worker, records = make(monkeypatch, events)
    worker.session_state.pipe_approval_timeout = 0.03 if decision == "timeout" else 3
    task = asyncio.create_task(worker.run())
    await worker.inbox.put({"type": "prompt", "id": "p", "content": "write"})
    await wait_for(lambda: any(r["type"] == "approval_required" for r in records))
    approval = next(r for r in records if r["type"] == "approval_required")
    if decision != "timeout":
        reply = {"type": "approval", "interrupt_id": approval["interrupt_id"], "decision": decision}
        await worker.inbox.put(reply)
        await wait_for(lambda: answers)
        await worker.inbox.put(reply)
        await wait_for(lambda: any(r.get("code") == "stale_interrupt" for r in records))
        assert answers[0]["decisions"][0]["type"] == decision
    await wait_for(lambda: any(r["type"] == "done" for r in records))
    if decision == "timeout":
        assert next(r for r in records if r["type"] == "done")["status"] == "approval_timeout"
    await worker.inbox.put({"type": "shutdown"})
    await task


@pytest.mark.asyncio
async def test_subagent_observability_frames(monkeypatch):
    worker, records = make(monkeypatch)

    async def events(text, *args, **kwargs):
        yield ev.SubagentTask(
            task_id="ptc_task_ab12",
            status="running",
            subagent_type="research",
            label="research: docling",
            phase_id="eval_1",
            phase_kind="eval",
        )
        yield ev.SubagentPreview("ptc_task_ab12", "text", "reading papers")
        yield ev.SubagentTask(
            task_id="ptc_task_ab12",
            status="done",
            duration_ms=42000,
        )
        yield ev.SubagentActivity(kind="completed", subagent_type="research")
        yield ev.Done(True)

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", events)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: records)
    await worker.inbox.put({"type": "prompt", "id": "p", "content": "fan out"})
    await wait_for(lambda: any(r["type"] == "done" for r in records))

    task_rows = [r for r in records if r["type"] == "subagent_task"]
    assert len(task_rows) == 2, "same task_id upserts, both emissions forwarded"
    assert task_rows[0]["status"] == "running"
    assert task_rows[0]["phase_kind"] == "eval"
    assert task_rows[1]["status"] == "done"
    assert task_rows[1]["duration_ms"] == 42000
    assert all(r["request_id"] == "p" for r in task_rows)

    previews = [r for r in records if r["type"] == "subagent_preview"]
    assert previews and previews[0]["text"] == "reading papers"
    assert previews[0]["task_id"] == "ptc_task_ab12"

    activities = [r for r in records if r["type"] == "subagent_activity"]
    assert activities and activities[0]["kind"] == "completed"
    assert activities[0]["subagent_type"] == "research"

    await worker.inbox.put({"type": "shutdown"})
    assert await task == 0


@pytest.mark.asyncio
async def test_request_deadline_then_next_prompt(monkeypatch):
    async def events(text, *args, **kwargs):
        if text == "slow":
            await asyncio.sleep(10)
        yield ev.Done(True)

    worker, records = make(monkeypatch, events)
    worker.session_state.pipe_request_timeout = 0.03
    task = asyncio.create_task(worker.run())
    await worker.inbox.put({"type": "prompt", "id": "slow", "content": "slow"})
    await worker.inbox.put({"type": "prompt", "id": "fast", "content": "fast"})
    await wait_for(lambda: len([r for r in records if r["type"] == "done"]) == 2)
    assert [r["status"] for r in records if r["type"] == "done"] == ["timeout", "success"]
    await worker.inbox.put({"type": "shutdown"})
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error", "exit"])
async def test_bootstrap_failure(monkeypatch, capsys, failure):
    worker, records = make(monkeypatch)

    async def startup():
        if failure == "timeout":
            await asyncio.sleep(10)
        elif failure == "exit":
            raise SystemExit(1)
        else:
            raise PermissionError("Folder not trusted")

    await run_pipe_bootstrap(startup(), worker.session_state)
    assert [r["type"] for r in records] == ["error", "stopped"]
    assert worker.session_state.headless_exit_code == (124 if failure == "timeout" else 1)


@pytest.mark.parametrize(
    "flags", [["--headless"], ["--mode", "pipe"], ["--mode", "pipe", "--input-format", "text"]]
)
def test_real_cli_persistent_pipe_shutdown_with_stdin_open(flags):
    script = """
import sys, os
from novacode_cli import main as cli, ui_events as ev
from novacode_cli.headless.pipe import run_pipe_session
import novacode_cli.sessions.worker as worker
cli.check_cli_dependencies = lambda: None
cli.settings.get_onboarding_status = lambda: True
async def start(agent, state, *args, **kwargs):
    async def events(text, *args, **kwargs):
        print("diagnostic noise")
        os.write(1, b"native noise\\n")
        yield ev.AssistantMessage(text, "Nova", "blue")
        yield ev.Done(True)
    worker.iterate_agent_events = events
    state.headless_exit_code = await run_pipe_session(agent=object(), assistant_id="nova", session_state=state)
cli.main = start
sys.argv = ["nova"] + sys.argv[1:]
cli.cli_main()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, *flags],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    records = []
    # communicate supplies EOF in text mode. JSONL explicitly sends shutdown
    # and intentionally keeps stdin open to exercise Windows reader teardown.
    if "text" in flags:
        out, err = process.communicate("测试\nsecond\n".encode(), timeout=20)
        records = [json.loads(line) for line in out.splitlines()]
        assert process.returncode == 0, err
        assert records[0]["type"] == "ready"
        return
    import queue
    import threading

    received = queue.Queue()
    stderr_lines = []

    def reader():
        for line in process.stdout:
            received.put(json.loads(line))

    threading.Thread(target=reader, daemon=True).start()

    def read_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    threading.Thread(target=read_stderr, daemon=True).start()
    try:
        try:
            ready = received.get(timeout=60)
        except queue.Empty as exc:
            raise AssertionError(f"pipe did not start: {b''.join(stderr_lines)[-2000:]!r}") from exc
        assert ready["type"] == "ready"
        for i in range(2):
            process.stdin.write(
                (json.dumps({"type": "prompt", "id": str(i), "content": "测试"}) + "\n").encode()
            )
            process.stdin.flush()
            while True:
                record = received.get(timeout=10)
                records.append(record)
                if record["type"] == "done":
                    assert record["ok"]
                    break
        process.stdin.write(b'{"type":"shutdown"}\n')
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        assert len([r for r in records if r["type"] == "text"]) == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.asyncio
async def test_ready_cancels_startup_timer_and_cleanup_error_is_final(monkeypatch):
    worker, records = make(monkeypatch)
    state = worker.session_state

    async def startup():
        worker._frame({"t": "ready"})
        await asyncio.sleep(0.08)
        raise RuntimeError("cleanup failed")

    await run_pipe_bootstrap(startup(), state)
    assert [r["type"] for r in records] == ["ready", "error", "stopped"]
    assert state.headless_exit_code == records[-1]["exit_code"] == 1


@pytest.mark.asyncio
async def test_cancel_and_eof_finish_active_and_queued(monkeypatch):
    async def events(*args, **kwargs):
        await asyncio.sleep(10)
        yield ev.Done(True)

    worker, records = make(monkeypatch, events)
    task = asyncio.create_task(worker.run())
    for pid in ("a", "b"):
        await worker.inbox.put({"type": "prompt", "id": pid, "content": "wait"})
    await wait_for(lambda: len(worker._queued) == 1)
    await worker.inbox.put({"type": "cancel"})
    await wait_for(lambda: len([r for r in records if r["type"] == "done"]) == 2)
    assert {r["request_id"] for r in records if r["type"] == "done"} == {"a", "b"}
    assert all(r["status"] == "cancelled" for r in records if r["type"] == "done")
    await worker.inbox.put(None)
    assert await task == 0


@pytest.mark.asyncio
async def test_fixed_policy_denial_survives_human_approve(monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.headless.pipe.evaluate_tool_actions",
        lambda *a, **k: [{"type": "reject"}, None],
    )
    worker, records = make(monkeypatch)
    worker._current_prompt = "test"
    worker._turn_id = "p"
    future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        worker._forward_interrupt(
            ev.InterruptRequest("tool", {"action_requests": [{"name": "a"}, {"name": "b"}]}, future)
        )
    )
    await wait_for(lambda: records)
    reply = worker.translate(
        {"type": "approval", "interrupt_id": records[-1]["interrupt_id"], "decision": "approve"}
    )
    worker._pending[reply["id"]].set_result(reply["result"])
    await task
    assert future.result() == {
        "decisions": [{"type": "reject"}, {"type": "approve"}],
        "any_rejected": True,
    }


@pytest.mark.asyncio
async def test_model_override_failure_never_falls_back(monkeypatch):
    from novacode_cli.main import _resolve_cli_model

    calls = []
    model = object()

    def build(provider, name):
        calls.append((provider, name))
        return model if name == "available" else None

    monkeypatch.setattr("novacode_cli.config.model_create.create_model_from_config", build)
    assert await _resolve_cli_model("custom:available") == (model, "custom")
    with pytest.raises(ValueError, match="Unable to build"):
        await _resolve_cli_model("custom:missing")
    assert calls == [("custom", "available"), ("custom", "missing")]


@pytest.mark.parametrize(
    "flags",
    [
        ["--headless", "-p", "hi"],
        ["--headless", "--resume"],
        ["--headless", "--timeout", "2"],
        ["--headless", "--approval-timeout", "0"],
        ["--headless", "--model", "bad"],
        ["--headless", "--session-worker"],
    ],
)
def test_invalid_pipe_cli_modes(monkeypatch, flags):
    from novacode_cli.main import parse_args

    monkeypatch.setattr(sys, "argv", ["nova", *flags])
    with pytest.raises(SystemExit) as error:
        parse_args()
    assert error.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,fatal",
    [
        (b"not json\n", False),
        (b"\xff\n", False),
        (b"[]\n", False),
        (b'{"type":"ping","id":NaN}\n', False),
        (b"[" * 2000 + b"]" * 2000 + b"\n", False),
        (b'{"type":"_input_error"}\n', False),
        (b"x" * (10 * 1024 * 1024 + 1), True),
    ],
    ids=["bad-json", "bad-utf8", "array", "nan", "deep-json", "reserved", "oversize"],
)
async def test_reader_bounds_and_bad_utf8(monkeypatch, raw, fatal):
    import io

    worker, records = make(monkeypatch)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    pump = asyncio.create_task(PipeSession._stdin_pump(worker))
    async with asyncio.timeout(3):
        frame = await worker.inbox.get()
    assert frame["type"] == "_input_error"
    assert bool(frame.get("fatal")) == fatal
    worker.translate(frame)
    assert records[-1]["code"] == "invalid_input"
    worker._stop = True
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump


@pytest.mark.parametrize("failure", ["dependencies", "onboarding"])
def test_real_cli_startup_failure_has_clean_json(failure):
    script = """
import sys
from novacode_cli import main as cli
def dependencies():
    print('diagnostic noise')
    if sys.argv[1] == 'dependencies':
        raise SystemExit(1)
cli.check_cli_dependencies = dependencies
cli.settings.get_onboarding_status = lambda: False
mode = sys.argv[1]
sys.argv = ['nova', '--mode', 'pipe']
if mode == 'dependencies':
    def missing():
        print('dependency diagnostic')
        raise SystemExit(1)
    cli.check_cli_dependencies = missing
else:
    cli.check_cli_dependencies = lambda: print('diagnostic noise')
cli.cli_main()
"""
    run = subprocess.run([sys.executable, "-c", script, failure], capture_output=True, timeout=60)
    assert run.returncode == 1
    records = [json.loads(line) for line in run.stdout.splitlines()]
    assert [r["type"] for r in records] == ["error", "stopped"]
    assert records[0]["code"] == (
        "missing_dependencies" if failure == "dependencies" else "not_configured"
    )
    assert b"diagnostic" in run.stderr
