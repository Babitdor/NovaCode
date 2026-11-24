"""Tests for /ralph UI emission — the handler routes all user-facing output
through the injected ``emit`` sink (so the TUI renders it natively) instead of
printing to stdout/stderr.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from novacode_cli.commands import ralph_handler as rh


@pytest.fixture(autouse=True)
def isolated_checkpoints(monkeypatch):
    monkeypatch.setattr(rh, "_get_checkpoint_path", lambda workspace: Path(workspace) / "ralph-checkpoint.json")


def test_a_workspace_can_only_have_one_checkpoint_owner(tmp_path):
    import os

    workspace = str(tmp_path)
    fd = rh._reserve_workspace(workspace)
    try:
        with pytest.raises(OSError):
            rh._reserve_workspace(workspace)
    finally:
        os.close(fd)
    os.close(rh._reserve_workspace(workspace))


def _sink():
    lines: list[str] = []
    return lines, lines.append


async def test_no_args_emits_usage_not_stdout(capsys):
    lines, emit = _sink()
    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=types.SimpleNamespace(),
        assistant_id="ralph",
        token_tracker=None,
        cmd_args=None,
        emit=emit,
    )
    assert ok is True
    joined = "\n".join(lines)
    assert "Usage: /ralph" in joined
    assert "--iterations" in joined
    # Nothing leaked to the real stdout/stderr.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


async def test_status_empty_emits_to_sink(capsys):
    lines, emit = _sink()
    session = types.SimpleNamespace(background_ralph_tasks={})
    ok = await rh.handle_ralph_status(session, emit)
    assert ok is True
    assert any("No background Ralph tasks" in ln for ln in lines)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


async def test_status_via_command_routes_to_status():
    lines, emit = _sink()
    session = types.SimpleNamespace(background_ralph_tasks={})
    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=session,
        assistant_id="ralph",
        token_tracker=None,
        cmd_args="--status",
        emit=emit,
    )
    assert ok is True
    assert any("Ralph Background Tasks" in ln for ln in lines)


async def test_bad_iterations_arg_emits_error():
    lines, emit = _sink()
    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=types.SimpleNamespace(),
        assistant_id="ralph",
        token_tracker=None,
        cmd_args="do something --iterations notanumber",
        emit=emit,
    )
    assert ok is True
    assert any("requires a number" in ln for ln in lines)


def test_console_emit_is_default():
    # The module's default sink is the console emitter (CLI path).
    assert rh._console_emit is not None
    assert rh.handle_ralph_command.__defaults__ is not None


# --- Structured on_event path (drives the TUI's native widgets) ------------


async def test_status_with_on_event_yields_snapshot():
    """--status hands a renderer a plain StatusSnapshot instead of markup lines."""
    from novacode_cli.commands import ralph_events as rev
    from novacode_cli.states.Session import BackgroundRalphTask, RalphTaskStatus

    task = BackgroundRalphTask(
        task_id="abc123",
        iteration=2,
        max_iterations=5,
        task_description="do the thing",
    )
    task.status = RalphTaskStatus.RUNNING
    session = types.SimpleNamespace(background_ralph_tasks={"abc123": task})

    markup, emit = _sink()
    events: list = []
    ok = await rh.handle_ralph_status(session, emit, on_event=events.append)

    assert ok is True
    # Nothing went to the markup sink — the renderer gets a structured snapshot.
    assert markup == []
    assert len(events) == 1
    snap = events[0]
    assert isinstance(snap, rev.StatusSnapshot)
    assert snap.total == 1
    assert snap.running == 1
    assert snap.rows[0].iteration == 2
    assert snap.rows[0].status == "running"


async def test_foreground_emits_structured_events(tmp_path, monkeypatch):
    """A foreground run reports start/iteration/finish through on_event, and the
    structured banner markup is suppressed when a renderer is wired up."""
    monkeypatch.chdir(tmp_path)

    calls: list[str] = []

    async def fake_execute(prompt, agent, name, ss, tt, backend=None):  # noqa: ANN001, ARG001
        assert not ss.auto_approve
        calls.append(name)

    session = types.SimpleNamespace(
        auto_approve=False,
        thread_id="t",
        workspace_root=str(tmp_path),
        background_ralph_tasks={},
        add_notification=lambda **_: None,
    )
    markup, emit = _sink()
    events: list = []

    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=session,
        assistant_id="ralph",
        token_tracker=None,
        cmd_args="do something --iterations 2",
        execute_fn=fake_execute,
        emit=emit,
        on_event=events.append,
    )

    assert ok is True
    assert calls == ["ralph", "ralph"]  # one agent run per iteration

    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "RalphStarted",
        "IterationStarted",
        "IterationFinished",
        "IterationStarted",
        "IterationFinished",
        "RalphFinished",
    ]
    assert events[0].task == "do something"
    assert events[0].max_iterations == 2
    assert events[0].background is False
    assert events[-1].completed == 2
    assert events[-1].failed == 0
    assert events[-1].reason == "limit"

    # The header/banner/summary markup is routed to on_event, not the markup sink.
    joined = "\n".join(markup)
    assert "Ralph Mode" not in joined
    assert "Iteration 1/2" not in joined


async def test_ralph_stop_requested(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    async def fake_execute(prompt, agent, name, ss, tt, backend=None):
        # Request stop when executing the first iteration
        ss._ralph_stop_requested = True
        calls.append(name)

    session = types.SimpleNamespace(
        auto_approve=False,
        thread_id="t",
        workspace_root=str(tmp_path),
        background_ralph_tasks={},
        add_notification=lambda **_: None,
        _ralph_stop_requested=False,
    )
    markup, emit = _sink()
    events = []

    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=session,
        assistant_id="ralph",
        token_tracker=None,
        cmd_args="do something --iterations 5",
        execute_fn=fake_execute,
        emit=emit,
        on_event=events.append,
    )

    assert ok is True
    # Should only execute one iteration, then stop.
    assert len(calls) == 1
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "RalphStarted",
        "IterationStarted",
        "IterationFinished",
        "RalphFinished",
    ]
    assert events[-1].completed == 1
    assert events[-1].reason == "stopped"


async def test_ralph_checkpoint_requested(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    async def fake_execute(prompt, agent, name, ss, tt, backend=None):
        # Request checkpoint
        ss._ralph_checkpoint_requested = True
        calls.append(name)

    session = types.SimpleNamespace(
        auto_approve=False,
        thread_id="t",
        workspace_root=str(tmp_path),
        background_ralph_tasks={},
        add_notification=lambda **_: None,
        _ralph_checkpoint_requested=False,
    )
    markup, emit = _sink()
    events = []

    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=session,
        assistant_id="ralph",
        token_tracker=None,
        cmd_args="do something --iterations 5",
        execute_fn=fake_execute,
        emit=emit,
        on_event=events.append,
    )

    assert ok is True
    # Should only execute one iteration, then stop after saving checkpoint.
    assert len(calls) == 1
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "RalphStarted",
        "IterationStarted",
        "IterationFinished",
        "RalphFinished",
    ]
    assert events[-1].completed == 1
    assert events[-1].reason == "checkpoint"
    # Ensure checkpoint file was created
    assert (tmp_path / "ralph-checkpoint.json").is_file()


async def test_ralph_cancelled_error(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.chdir(tmp_path)
    calls = []

    async def fake_execute(prompt, agent, name, ss, tt, backend=None):
        calls.append(name)
        raise asyncio.CancelledError()

    session = types.SimpleNamespace(
        auto_approve=False,
        thread_id="t",
        workspace_root=str(tmp_path),
        background_ralph_tasks={},
        add_notification=lambda **_: None,
    )
    markup, emit = _sink()
    events = []

    ok = await rh.handle_ralph_command(
        agent=object(),
        session_state=session,
        assistant_id="ralph",
        token_tracker=None,
        cmd_args="do something --iterations 5",
        execute_fn=fake_execute,
        emit=emit,
        on_event=events.append,
    )

    assert ok is True
    assert len(calls) == 1
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "RalphStarted",
        "IterationStarted",
        "RalphFinished",
    ]
    assert events[-1].completed == 0
    assert events[-1].reason == "stopped"


async def test_ralph_new_run_cannot_inherit_completed_work_and_resume_keeps_its_files(tmp_path):
    import re

    from novacode_cli.commands.ralph_events import RalphStarted

    first, other = tmp_path / "first", tmp_path / "other"
    first.mkdir()
    other.mkdir()
    session = types.SimpleNamespace(auto_approve=False, thread_id="t", workspace_root=str(first))
    events, paths = [], []

    async def execute(prompt, *_args, **_kwargs):
        checklist = first / re.search(r"\.nova/ralph/runs/[0-9a-f]+/checklist\.md", prompt)[0]
        paths.append(checklist)
        complete = len(paths) != 2
        checklist.write_text(f"- [{'x' if complete else ' '}] Check the work\n", encoding="utf-8")
        if not complete:
            checklist.with_name("progress.md").write_text("Notes from task two", encoding="utf-8")
        if len(paths) == 3:
            assert "Notes from task two" in prompt

    async def run(command):
        await rh.handle_ralph_command(object(), session, "ralph", None, command, execute_fn=execute,
                                      emit=lambda _message: None, on_event=events.append)

    await run("first task --iterations 1")
    assert events[-1].reason == "complete"
    await run("second task --iterations 1")
    assert events[-1].reason == "limit"
    assert paths[0] != paths[1]
    session.workspace_root = str(other)
    await run("--resume")
    assert len(paths) == 2
    session.workspace_root = str(first)
    await run("--resume")
    assert paths[1] == paths[2]
    assert [item.resumed_from for item in events if isinstance(item, RalphStarted)] == [None, None, 2]
    assert events[-1].reason == "complete"


async def test_ralph_verify_runs_the_original_command_and_gates_completion(tmp_path):
    import re
    import sys

    verify = tmp_path / rh.RALPH_VERIFY_PATH
    verify.parent.mkdir(parents=True)
    verify.write_text(f'"{sys.executable}" -c "raise SystemExit(7)"', encoding="utf-8")
    session = types.SimpleNamespace(auto_approve=False, thread_id="t", workspace_root=str(tmp_path))
    events = []

    async def execute(prompt, *_args, **_kwargs):
        checklist = tmp_path / re.search(r"\.nova/ralph/runs/[0-9a-f]+/checklist\.md", prompt)[0]
        checklist.write_text("- [x] Implement the change\n", encoding="utf-8")
        verify.write_text(f'"{sys.executable}" -c "raise SystemExit(0)"', encoding="utf-8")

    await rh.handle_ralph_command(object(), session, "ralph", None, "test --iterations 1 --verify",
                                  execute_fn=execute, emit=lambda _message: None, on_event=events.append)
    assert events[-1].reason == "limit"
    assert "verification still failing" in events[-1].detail
    assert not rh._load_ralph_checkpoint(str(tmp_path.resolve())).get("complete")


async def test_background_ralph_requires_permission_and_uses_the_same_completion_policy(tmp_path, monkeypatch):
    import asyncio
    import re
    import threading

    from novacode_cli.commands.ralph_events import RalphFinished
    from novacode_cli.ui import execution

    session = types.SimpleNamespace(auto_approve=False, thread_id="t", workspace_root=str(tmp_path),
                                    background_ralph_tasks={})
    messages, events = [], []
    finished = threading.Event()

    async def execute(prompt, *_args, is_subagent, **_kwargs):
        assert is_subagent and session.auto_approve
        checklist = tmp_path / re.search(r"\.nova/ralph/runs/[0-9a-f]+/checklist\.md", prompt)[0]
        checklist.write_text("- [x] Work done\n", encoding="utf-8")
        return True

    def report(event):
        events.append(event)
        if isinstance(event, RalphFinished):
            finished.set()

    monkeypatch.setattr(execution, "execute_task", execute)
    await rh.handle_ralph_command(object(), session, "ralph", None, "do it --background",
                                  emit=messages.append, on_event=report)
    assert not events and not session.auto_approve
    assert "already enabled" in messages[-1]
    session.auto_approve = True
    await rh.handle_ralph_command(object(), session, "ralph", None, "do it --background",
                                  emit=messages.append, on_event=report)
    assert await asyncio.to_thread(finished.wait, 5)
    assert events[-1].reason == "complete"
    assert sum(type(event).__name__ == "RalphStarted" for event in events) == 1
    assert session.auto_approve


async def test_background_stop_cancels_the_active_iteration_and_keeps_checkpoint(
    tmp_path, monkeypatch
):
    import asyncio
    import threading

    from novacode_cli.commands.ralph_events import RalphFinished
    from novacode_cli.shell.jobs import ralph_stop_signal
    from novacode_cli.ui import execution

    session = types.SimpleNamespace(
        auto_approve=True, thread_id="t", workspace_root=str(tmp_path), background_ralph_tasks={}
    )
    started = threading.Event()
    finished = threading.Event()
    events = []
    signals = []

    async def execute(*_args, **_kwargs):
        signal = ralph_stop_signal.get()
        signals.append(signal)
        started.set()
        await asyncio.Future()

    def report(event):
        events.append(event)
        if isinstance(event, RalphFinished):
            finished.set()

    monkeypatch.setattr(execution, "execute_task", execute)
    await rh.handle_ralph_command(
        object(), session, "ralph", None, "do it --background",
        emit=lambda _message: None, on_event=report,
    )
    assert await asyncio.to_thread(started.wait, 5)
    assert rh.request_ralph_stop(session)
    assert await asyncio.to_thread(finished.wait, 5)
    assert signals[0].is_set()
    assert len(signals) == 1
    assert events[-1].reason == "stopped"
    assert events[-1].completed == 0
    assert rh._load_ralph_checkpoint(str(tmp_path))["completed_iterations"] == 0

