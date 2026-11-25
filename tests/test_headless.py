"""Tests for headless (non-interactive) mode.

Headless is non-Textual, so unlike the TUI it is unit-testable: the formatter,
the interrupt resolver, prompt resolution, and the run loop (with a fake event
source) all run headless under pytest.
"""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

from novacode_cli import ui_events as ev
from novacode_cli.headless.output import HeadlessOutput
from novacode_cli.headless.runner import (
    EXIT_ERROR,
    EXIT_MAX_TURNS,
    EXIT_OK,
    _resolve_interrupt,
    run_headless,
)


# --------------------------------------------------------------------------- #
# Output formatting
# --------------------------------------------------------------------------- #
def _make_output(fmt: str) -> tuple[HeadlessOutput, io.StringIO]:
    buf = io.StringIO()
    return HeadlessOutput(fmt, session_id="sess-1", model_name="claude-x", stream=buf), buf


def test_text_format_emits_only_result() -> None:
    out, buf = _make_output("text")
    out.init()
    out.handle_event(ev.AssistantMessage(text="hello", agent_name="Nova", agent_color="x"))
    out.handle_event(ev.ToolCall(name="execute", display_str="ls", icon=">"))
    out.result(
        subtype="success",
        is_error=False,
        result_text="final answer",
        num_turns=1,
        duration_ms=5,
        usage={},
    )
    # text mode: per-event output suppressed, only the final answer on stdout.
    assert buf.getvalue() == "final answer\n"


def test_json_format_single_result_object() -> None:
    out, buf = _make_output("json")
    out.init()
    out.handle_event(ev.AssistantMessage(text="x", agent_name="Nova", agent_color="x"))
    out.result(
        subtype="success",
        is_error=False,
        result_text="the answer",
        num_turns=2,
        duration_ms=42,
        usage={"input_tokens": 10, "output_tokens": 3},
    )
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["type"] == "result"
    assert obj["subtype"] == "success"
    assert obj["is_error"] is False
    assert obj["result"] == "the answer"
    assert obj["session_id"] == "sess-1"
    assert obj["num_turns"] == 2
    assert obj["usage"]["input_tokens"] == 10


def test_stream_json_emits_init_events_and_result() -> None:
    out, buf = _make_output("stream-json")
    out.init()
    out.handle_event(ev.AssistantMessage(text="hi", agent_name="Nova", agent_color="x"))
    out.handle_event(
        ev.ToolCall(name="execute", display_str="ls", icon=">", args={"cmd": "ls"}, call_id="t1")
    )
    out.handle_event(
        ev.ToolResult(preview="ok", is_error=False, full_output="file.txt", call_id="t1")
    )
    out.result(
        subtype="success",
        is_error=False,
        result_text="done",
        num_turns=1,
        duration_ms=1,
        usage={},
    )
    objs = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    types = [o["type"] for o in objs]
    assert types[0] == "system"
    assert objs[0]["subtype"] == "init"
    assert "assistant" in types
    assert "tool_use" in types
    assert "tool_result" in types
    assert types[-1] == "result"


def test_invalid_format_raises() -> None:
    with pytest.raises(ValueError, match="Unknown output format"):
        HeadlessOutput("yaml", session_id="s", model_name=None)


# --------------------------------------------------------------------------- #
# Interrupt resolution
# --------------------------------------------------------------------------- #
async def test_tool_interrupt_auto_approves_when_auto_approve() -> None:
    session_state = SimpleNamespace(auto_approve=True, plan_mode_enabled=False)
    payload = {
        "action_requests": [{"name": "execute", "args": {}}, {"name": "write_file", "args": {}}]
    }
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    event = ev.InterruptRequest(kind="tool", payload=payload, future=fut)

    await _resolve_interrupt(event, session_state, deny_tools=False)

    result = await fut
    assert result["any_rejected"] is False
    assert [d["type"] for d in result["decisions"]] == ["approve", "approve"]


async def test_tool_interrupt_rejects_when_deny_tools() -> None:
    session_state = SimpleNamespace(auto_approve=True, plan_mode_enabled=False)
    payload = {"action_requests": [{"name": "execute", "args": {}}]}
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    event = ev.InterruptRequest(kind="tool", payload=payload, future=fut)

    await _resolve_interrupt(event, session_state, deny_tools=True)

    result = await fut
    assert result["any_rejected"] is True


async def test_question_interrupt_resolves_benign_default() -> None:
    session_state = SimpleNamespace(auto_approve=True, plan_mode_enabled=False)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    event = ev.InterruptRequest(kind="question", payload={}, future=fut)

    await _resolve_interrupt(event, session_state, deny_tools=False)

    assert await fut == {}


# --------------------------------------------------------------------------- #
# run_headless loop (fake event source)
# --------------------------------------------------------------------------- #
def _fake_source(events: list) -> Callable[..., AsyncIterator]:
    """Return a no-arg async-generator factory yielding the given events."""

    async def _gen(*_args: object, **_kwargs: object) -> AsyncIterator:
        for e in events:
            yield e

    return _gen


def _headless_state(
    fmt: str = "json",
    *,
    max_turns: int | None = None,
    deny_tools: bool = False,
    prompt: str = "do the thing",
) -> SimpleNamespace:
    return SimpleNamespace(
        headless_prompt=prompt,
        headless_output_format=fmt,
        headless_max_turns=max_turns,
        headless_deny_tools=deny_tools,
        session_id="sess-1",
        thread_id="thread-1",
        plan_mode_enabled=False,
        auto_approve=True,
        todos=None,
    )


async def test_run_headless_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = [
        ev.AssistantMessage(text="The answer is 4.", agent_name="Nova", agent_color="x"),
        ev.UsageUpdate(input_tokens=12, output_tokens=5),
        ev.Done(had_response=True),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))

    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json"),
        session_manager=None,  # skip autosave
    )
    assert code == EXIT_OK
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["subtype"] == "success"
    assert obj["result"] == "The answer is 4."
    assert obj["usage"]["input_tokens"] == 12


async def test_run_headless_error_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = [ev.Error(message="boom", is_provider_notice=False)]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))

    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json"),
        session_manager=None,
    )
    assert code == EXIT_ERROR
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["is_error"] is True
    assert obj["subtype"] == "error_during_execution"


async def test_run_headless_max_turns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Three assistant turns but a cap of 1 -> a later turn trips the cap.
    events = [
        ev.AssistantMessage(text="step 1", agent_name="Nova", agent_color="x"),
        ev.AssistantMessage(text="step 2", agent_name="Nova", agent_color="x"),
        ev.AssistantMessage(text="step 3", agent_name="Nova", agent_color="x"),
        ev.Done(had_response=True),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))

    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json", max_turns=1),
        session_manager=None,
    )
    assert code == EXIT_MAX_TURNS
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["subtype"] == "error_max_turns"
    assert obj["is_error"] is True


# --------------------------------------------------------------------------- #
# Slash commands in one-shot runs
# --------------------------------------------------------------------------- #
async def test_run_headless_slash_command_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def dispatch(*args):
        return True, "Effort set to high", None, False

    monkeypatch.setattr("novacode_cli.headless.runner._dispatch_command", dispatch)
    called = False

    def _guard(*_args: object, **_kwargs: object) -> AsyncIterator:
        nonlocal called
        called = True
        yield ev.Done(had_response=True)

    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _guard)

    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json", prompt="/effort high"),
        session_manager=None,
    )
    # A command without kickoff must not start an agent turn.
    assert called is False
    assert code == EXIT_OK
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["subtype"] == "command"
    assert obj["command"] == "/effort high"
    assert obj["is_error"] is False
    assert obj["result"] == "Effort set to high"


async def test_run_headless_slash_command_kickoff_runs_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def dispatch(*args):
        return True, "Goal set: check tests", "check the tests", False

    monkeypatch.setattr("novacode_cli.headless.runner._dispatch_command", dispatch)
    events = [
        ev.AssistantMessage(text="Tests pass.", agent_name="Nova", agent_color="x"),
        ev.Done(had_response=True),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))

    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json", prompt="/goal check tests"),
        session_manager=None,
    )
    assert code == EXIT_OK
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["subtype"] == "success"
    assert obj["result"] == "Tests pass."
    assert obj["command"] == "/goal check tests"


async def test_run_headless_unknown_command_is_structured_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def dispatch(*args):
        return False, "Unknown command: /typo — run `nova commands --json`", None, False

    monkeypatch.setattr("novacode_cli.headless.runner._dispatch_command", dispatch)

    code = await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json", prompt="/typo"),
        session_manager=None,
    )
    assert code == EXIT_ERROR
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["subtype"] == "command"
    assert obj["is_error"] is True
    assert "Unknown command" in obj["result"]


async def test_result_includes_tool_call_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = [
        ev.ToolCall(name="read_file", display_str="read", icon=">", call_id="t1"),
        ev.ToolResult(preview="ok", is_error=False, full_output="x", call_id="t1"),
        ev.ToolCall(name="execute", display_str="ls", icon=">", call_id="t2"),
        ev.ToolResult(preview="ok", is_error=False, full_output="y", call_id="t2"),
        ev.AssistantMessage(text="done", agent_name="Nova", agent_color="x"),
        ev.Done(had_response=True),
    ]
    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", _fake_source(events))

    await run_headless(
        agent=object(),
        assistant_id="nova-agent",
        session_state=_headless_state("json"),
        session_manager=None,
    )
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["tool_call_count"] == 2


# --------------------------------------------------------------------------- #
# Command-surface discovery (`nova commands --json`)
# --------------------------------------------------------------------------- #
def test_commands_catalog_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    from novacode_cli.main import run_commands_catalog

    run_commands_catalog(use_json=True)
    catalog = json.loads(capsys.readouterr().out)
    assert catalog, "the catalogue must not be empty"
    entry = catalog[0]
    assert {"name", "description", "route", "headless", "aliases"} <= set(entry)
    assert any(item["headless"] for item in catalog)


def test_commands_catalog_prints_table(capsys: pytest.CaptureFixture[str]) -> None:
    from novacode_cli.main import run_commands_catalog

    run_commands_catalog()
    out = capsys.readouterr().out
    assert "/effort" in out
    assert "headless" in out


# --------------------------------------------------------------------------- #
# Prompt resolution (main.py)
# --------------------------------------------------------------------------- #
def test_resolve_prompt_from_string() -> None:
    from novacode_cli.main import _resolve_headless_prompt

    assert _resolve_headless_prompt("  hello world  ") == "hello world"


def test_resolve_prompt_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import main as main_mod

    fake_stdin = SimpleNamespace(isatty=lambda: False, read=lambda size: "piped prompt\n")
    monkeypatch.setattr(main_mod.sys, "stdin", fake_stdin)
    # Bare -p -> print_arg is True -> read stdin.
    bare_flag = True
    assert main_mod._resolve_headless_prompt(bare_flag) == "piped prompt"


def test_resolve_prompt_missing_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import main as main_mod

    fake_stdin = SimpleNamespace(isatty=lambda: True, read=lambda: "")
    monkeypatch.setattr(main_mod.sys, "stdin", fake_stdin)
    bare_flag = True
    with pytest.raises(SystemExit):
        main_mod._resolve_headless_prompt(bare_flag)
