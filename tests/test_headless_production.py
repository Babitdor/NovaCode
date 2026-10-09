"""Unattended pipe contracts, including real subprocess stdout separation."""

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.headless.input import (
    MAX_PROMPT_SIZE,
    positive_int,
    positive_seconds,
    read_prompt,
    require_workspace_approval,
)
from novacode_cli.headless.output import HeadlessOutput
from novacode_cli.headless.runner import run_bootstrap, run_headless, _resolve_interrupt
from tests.test_headless import _fake_source, _headless_state


def test_bounded_utf8_stdin_and_explicit_empty_prompt():
    stdin = SimpleNamespace(
        isatty=lambda: False, buffer=io.BytesIO(b"\xef\xbb\xbfFix \xe6\xb5\x8b\xe8\xaf\x95\n")
    )
    assert read_prompt(True, stdin) == "Fix 测试"
    for raw, match in (
        (b"\xff", "UTF-8"),
        (b"x" * (MAX_PROMPT_SIZE + 1), "1 MiB"),
        (b"  ", "non-empty"),
    ):
        stdin.buffer = io.BytesIO(raw)
        with pytest.raises(ValueError, match=match):
            read_prompt(True, stdin)
    stdin.buffer = Mock()
    with pytest.raises(ValueError, match="non-empty"):
        read_prompt("", stdin)
    stdin.buffer.read.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc"])
def test_deadline_validation(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_seconds(value)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc"])
def test_turn_budget_validation(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(value)


def test_workspace_trust_requires_explicit_permission(tmp_path):
    manager = Mock()
    manager.is_path_approved.return_value = False
    with pytest.raises(PermissionError):
        require_workspace_approval(manager, tmp_path)
    manager.approve_path.assert_not_called()
    require_workspace_approval(manager, tmp_path, explicit_trust=True)
    manager.approve_path.assert_called_once_with(tmp_path, recursive=False)
    manager.reset_mock()
    manager.is_path_approved.return_value = True
    require_workspace_approval(manager, tmp_path)
    manager.approve_path.assert_not_called()


def test_partial_writes_and_interrupted_write_preserve_unicode(monkeypatch):
    written = bytearray()
    first = True

    def write(fd, data):
        nonlocal first
        if first:
            first = False
            raise InterruptedError()
        chunk = data[:3]
        written.extend(chunk)
        return len(chunk)

    monkeypatch.setattr(os, "write", write)
    out = HeadlessOutput("stream-json", "session", None, fd=99)
    out.init()
    out.handle_event(ev.AssistantMessage("测试", "Nova", "x"))
    records = [json.loads(line) for line in written.decode("utf-8").splitlines()]
    assert records[-1]["text"] == "测试"
    assert all(record["schema_version"] == 1 for record in records)


def test_partial_stream_events_are_explicit_and_discardable():
    stream = io.StringIO()
    out = HeadlessOutput("stream-json", "session", None, stream, include_partial_messages=True)
    out.handle_event(ev.TextDelta("temporary"))
    out.handle_event(ev.TextDiscard())
    assert [json.loads(line)["type"] for line in stream.getvalue().splitlines()] == [
        "text_delta",
        "text_discard",
    ]


@pytest.mark.asyncio
async def test_unresolved_approval_rejects_and_reports_missing_human(monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.headless.runner.evaluate_tool_actions", lambda *a, **kw: [None]
    )
    state = _headless_state()
    future = asyncio.get_running_loop().create_future()
    request = ev.InterruptRequest("tool", {"action_requests": [{"name": "execute"}]}, future)
    assert await _resolve_interrupt(request, state, deny_tools=False)
    assert future.result()["any_rejected"]
    monkeypatch.setattr(
        "novacode_cli.headless.runner.iterate_agent_events", _fake_source([request, ev.Done(True)])
    )
    assert await run_headless(agent=object(), assistant_id="nova", session_state=state) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, expected",
    [("timeout", 124), ("provider_timeout", 1), ("cancel", 130), ("incomplete", 1)],
)
async def test_deadline_cancel_and_truncated_stream(monkeypatch, capsys, mode, expected):
    closed = []

    async def source(*args, **kwargs):
        try:
            if mode == "cancel":
                raise asyncio.CancelledError()
            if mode == "provider_timeout":
                raise TimeoutError("Provider network timeout")
            if mode == "timeout":
                await asyncio.sleep(60)
            if False:
                yield
        finally:
            closed.append(True)

    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", source)
    state = _headless_state()
    state.headless_timeout = 0.02 if mode == "timeout" else None
    code = await run_headless(agent=object(), assistant_id="nova", session_state=state)
    assert code == expected and closed == [True]
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == expected and result["is_error"]


@pytest.mark.asyncio
async def test_subagent_output_does_not_pollute_final_answer_or_budget(monkeypatch, capsys):
    events = [
        ev.AssistantMessage("child output", "Child", "x", True),
        ev.AssistantMessage("Final", "Nova", "x"),
        ev.Done(True),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))
    assert (
        await run_headless(
            agent=object(), assistant_id="nova", session_state=_headless_state(max_turns=1)
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "Final" and result["num_turns"] == 1


@pytest.mark.asyncio
async def test_tool_only_rounds_cannot_evade_turn_limit(monkeypatch, capsys):
    events = [
        ev.ToolCall("read_file", "read", "", call_id="first"),
        ev.ToolResult("ok", call_id="first"),
        ev.ToolCall("read_file", "read", "", call_id="second"),
        ev.Done(True),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))
    state = _headless_state(max_turns=1)
    assert await run_headless(agent=object(), assistant_id="nova", session_state=state) == 2
    assert json.loads(capsys.readouterr().out)["subtype"] == "error_max_turns"


@pytest.mark.asyncio
async def test_bootstrap_deadline_during_agent_run_emits_once(monkeypatch, capsys):
    async def source(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            yield ev.Cancelled()

    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", source)
    state = _headless_state()
    state.headless_timeout = 0.02

    async def start():
        state.headless_exit_code = await run_headless(
            agent=object(), assistant_id="nova", session_state=state
        )

    await run_bootstrap(start(), state)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1 and records[0]["exit_code"] == 124
    assert state.headless_exit_code == 124


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "error", "exit"])
async def test_bootstrap_failures_have_one_structured_result(capsys, mode):
    async def start():
        if mode == "timeout":
            await asyncio.sleep(60)
        elif mode == "exit":
            raise SystemExit(1)
        else:
            raise PermissionError("Not approved")

    state = _headless_state()
    state.headless_timeout = 0.02 if mode == "timeout" else None
    await run_bootstrap(start(), state)
    records = capsys.readouterr().out.splitlines()
    assert len(records) == 1
    result = json.loads(records[0])
    assert result["is_error"] and result["exit_code"] == state.headless_exit_code


@pytest.mark.asyncio
async def test_closed_pipe_is_bounded_and_returns_pipe_failure(monkeypatch):
    def broken(*args):
        raise BrokenPipeError()

    monkeypatch.setattr(os, "write", broken)
    monkeypatch.setattr(
        "novacode_cli.headless.runner.iterate_agent_events", _fake_source([ev.Done(True)])
    )
    state = _headless_state("stream-json")
    state.headless_out_fd = 99
    assert await run_headless(agent=object(), assistant_id="nova", session_state=state) == 141


@pytest.mark.asyncio
async def test_failed_diagnostic_stream_does_not_hide_startup_result(monkeypatch, capsys):
    monkeypatch.setattr(
        "novacode_cli.headless.runner.console.print", Mock(side_effect=ValueError("closed stderr"))
    )

    async def start():
        raise RuntimeError("startup failed")

    state = _headless_state()
    await run_bootstrap(start(), state)
    assert state.headless_exit_code == 1
    assert json.loads(capsys.readouterr().out)["subtype"] == "error_during_initialization"


@pytest.mark.asyncio
async def test_cleanup_timeout_does_not_contradict_flushed_result(monkeypatch, capsys):
    monkeypatch.setattr(
        "novacode_cli.headless.runner.iterate_agent_events", _fake_source([ev.Done(True)])
    )
    state = _headless_state()
    state.headless_timeout = 0.02

    async def start():
        await run_headless(agent=object(), assistant_id="nova", session_state=state)
        await asyncio.sleep(60)

    await run_bootstrap(start(), state)
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == state.headless_exit_code == 0


@pytest.mark.parametrize(
    "flags, approved",
    [([], False), (["--auto-approve"], True), (["--auto-approve", "--deny-tools"], False)],
)
def test_real_cli_pipe_wires_explicit_approval_and_utf8_input(flags, approved):
    script = """
import asyncio, json, sys
from novacode_cli import main as cli
from novacode_cli import ui_events as ev
from novacode_cli.headless import runner
cli.check_cli_dependencies = lambda: None
cli.settings.get_onboarding_status = lambda: True
async def start(agent, state, *args, **kwargs):
    async def events(*args, **kwargs):
        yield ev.AssistantMessage(json.dumps({"approved":state.auto_approve,"prompt":state.headless_prompt}), "Nova", "x")
        yield ev.Done(True)
    runner.iterate_agent_events = events
    state.headless_exit_code = await runner.run_headless(agent=object(), assistant_id="nova", session_state=state)
cli.main = start
sys.argv = ["nova", "-p", "--output-format", "json", "--timeout", "5"] + sys.argv[1:]
cli.cli_main()
"""
    run = subprocess.run(
        [sys.executable, "-c", script, *flags],
        input="测试 task".encode("utf-8"),
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
    result = json.loads(run.stdout.decode("utf-8"))
    assert result["exit_code"] == 0
    assert json.loads(result["result"]) == {"approved": approved, "prompt": "测试 task"}


@pytest.mark.parametrize(
    "flags",
    [
        ["-p", "--resume"],
        ["-p", "--timeout", "0"],
        ["-p", "--max-turns", "-1"],
        ["-p", "--include-partial-messages"],
        ["--timeout", "5"],
    ],
)
def test_invalid_cli_modes_fail_before_startup(monkeypatch, flags):
    from novacode_cli.main import parse_args

    monkeypatch.setattr(sys, "argv", ["nova", *flags])
    with pytest.raises(SystemExit) as error:
        parse_args()
    assert error.value.code == 2


def test_real_closed_consumer_returns_pipe_failure():
    script = """
import asyncio, os
from types import SimpleNamespace
from novacode_cli import ui_events as ev
from novacode_cli.main import _setup_headless_io
from novacode_cli.headless import runner
fd = _setup_headless_io()
async def events(*args, **kwargs):
    yield ev.AssistantMessage("x" * 1048576, "Nova", "x")
    yield ev.Done(True)
runner.iterate_agent_events = events
state = SimpleNamespace(headless_prompt="test", headless_output_format="json", session_id="test", headless_out_fd=fd)
code = asyncio.run(runner.run_headless(agent=object(), assistant_id="nova", session_state=state))
os._exit(code)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    process.stdout.close()
    process.stdout = None
    try:
        _, error = process.communicate(timeout=20)
        assert process.returncode == 141, error.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_pipe_keeps_native_and_child_output_on_stderr():
    script = """
import os, subprocess, sys
from novacode_cli.main import _setup_headless_io
from novacode_cli.headless.output import HeadlessOutput
fd = _setup_headless_io()
print("python noise")
os.write(1, b"native noise\\n")
subprocess.run([sys.executable, "-c", "print('child noise')"], check=True)
out = HeadlessOutput("json", "session", None, fd=fd)
out.result(subtype="success", is_error=False, result_text="测试", num_turns=1, duration_ms=1, usage={})
os.close(fd)
"""
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
    result = json.loads(run.stdout.decode("utf-8"))
    assert result["result"] == "测试"
    assert (
        b"python noise" in run.stderr
        and b"native noise" in run.stderr
        and b"child noise" in run.stderr
    )
