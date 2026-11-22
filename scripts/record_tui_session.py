#!/usr/bin/env python
"""Record a deterministic TUI session as JSONL frames.

The frames come out of the *production* path — ``SessionHost`` emitting through
the same ``frames`` builders and ``sessions.protocol`` encoder the server uses —
so replaying the file exercises what a real client receives. Only the agent is
fake: a scripted event stream stands in for ``iterate_agent_events``, which means
no model, no API key, no network spend, and byte-identical output on every run.

Usage::

    uv run python scripts/record_tui_session.py --out rust/nova-tui/tests/fixtures/core_chat.jsonl

The Rust replay test (``rust/nova-tui/tests/frames_replay.rs``) treats the file as
the parity corpus, so a change here is a deliberate change to what "ported" means.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Where the replay corpora live, one file per scenario.
FIXTURES = Path("rust/nova-tui/tests/fixtures")

# The recorded session's identity. Fixed strings, so the recording does not
# depend on the machine, the clock or the working directory.
SESSION_ID = "recorded-session"
MODEL_NAME = "recording-model"
CWD = "C:/work/nova"
PROMPT = "Run the tests and tell me what broke."

# The tool output the replay test asserts on: a preview, a body, and a final
# line, so "the last line of a tool result is visible" is testable.
TOOL_OUTPUT = (
    "collected 28 items\n"
    "tests/test_alpha.py .......  [ 25%]\n"
    "tests/test_beta.py .......  [ 50%]\n"
    "tests/test_gamma.py ....... [ 75%]\n"
    "tests/test_delta.py .......  [100%]\n"
    "final line: 28 passed"
)

ASSISTANT_MARKDOWN = """# Plan

1. Read the failing test
2. Run `pytest -q`
3. Report the *delta* between runs

Nothing is broken yet — verify first."""

CLOSING_TEXT = "All green: 28 passed. Nothing to fix."

# The screens scenario replays every kind of the vocabulary in
# `novacode_cli/ui/port.ScreenSpec` through the production `screen` envelope, so
# the Rust renderer and the Python spec builders cannot drift apart silently.
SCREENS: tuple[tuple[str, str, dict[str, Any], Any], ...] = (
    (
        "list",
        "Resume a session",
        {
            "items": [
                {"id": "s1", "label": "nova - port the TUI", "detail": "2 hours ago"},
                {"id": "s2", "label": "nova - fix flaky test", "detail": "yesterday"},
            ]
        },
        {"id": "s1"},
    ),
    (
        "table",
        "Background tasks",
        {
            "columns": [
                {"key": "id", "title": "Task", "width": 8},
                {"key": "status", "title": "Status", "width": 10},
            ],
            "rows": [{"id": "t1", "task": "t1", "status": "running"}],
        },
        {"id": "t1"},
    ),
    ("confirm", "Delete session?", {"message": "This cannot be undone."}, True),
    (
        "form",
        "Model settings",
        {
            "fields": [
                {"key": "model", "label": "Model", "value": "gpt-5", "kind": "text"},
                {"key": "effort", "label": "Effort", "value": "medium", "kind": "choice"},
            ],
            "submit": "Save",
        },
        {"model": "gpt-5", "effort": "medium"},
    ),
    (
        "progress",
        "Reindex",
        {"label": "indexing workspace", "done": 3, "total": 10},
        None,
    ),
    (
        "detail",
        "Session report",
        {
            "markdown": "# Session report\n\n- 12 turns\n- 4 tools\n",
            "actions": [{"id": "resume", "label": "Resume"}, {"id": "delete", "label": "Delete"}],
        },
        {"action": "resume"},
    ),
)


class _Breakdown:
    """A token breakdown shaped like TokenTracker.get_breakdown().

    Fixed numbers, so the recorded /context report is byte-identical on every
    machine; a change here is a deliberate change to the corpus.
    """

    def __init__(self) -> None:
        self.context_window_size = 200_000
        self.total_tokens = 12_345
        self.system_prompt_tokens = 4_000
        self.user_memory_tokens = 1_000
        self.project_memory_tokens = 500
        self.tool_definitions_tokens = 2_000
        self.user_message_tokens = 1_500
        self.assistant_message_tokens = 2_000
        self.tool_result_tokens = 1_345
        self.mcp_tool_tokens = 0
        self.mcp_prompt_tokens = 0
        self.mcp_tool_count = 0
        self.user_message_count = 3
        self.assistant_message_count = 3
        self.tool_call_count = 2


class _Tracker:
    """A token tracker stand-in with the attributes the reports read."""

    def __init__(self) -> None:
        self.model_name = MODEL_NAME
        self.has_api_data = False
        self.last_output = 321
        self.session_total_tokens = 1_555
        self.last_total_tokens = 12_345
        self.context_window_size = 200_000
        self.context_snapshot = None
        self._breakdown = _Breakdown()

    def get_breakdown(self) -> _Breakdown:
        """Return the fixed breakdown."""
        return self._breakdown


class _SessionState:
    """Minimal session-state stand-in.

    The server never reads a real ``SessionState`` for these fields; the worker
    only needs identity, steering storage and the notification hook.
    """

    def __init__(self, *, tracker: bool = False, session_id: str = SESSION_ID) -> None:
        self.thread_id = "recorded-thread" if session_id == SESSION_ID else f"thread-{session_id}"
        self.session_id = session_id
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos: list[dict[str, Any]] = [
            {"content": "Read the failing test", "status": "completed"},
            {"content": "Run pytest -q", "status": "in_progress"},
        ]
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None
        if tracker:
            self.token_tracker = _Tracker()


def _scripted_stream() -> Any:  # noqa: ANN401 — an async generator factory
    """Build the scripted turn: streaming text, a tool call, an approval, a close."""
    from novacode_cli import ui_events as ev

    async def stream(_text: str, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield ev.TextDelta("Reading the suite first.")
        yield ev.ReasoningDelta("The user wants the failures, not a summary.")
        yield ev.AssistantMessage(ASSISTANT_MARKDOWN, "Nova", "cyan")
        yield ev.TodoUpdate(
            [
                {"content": "Read the failing test", "status": "completed"},
                {"content": "Run pytest -q", "status": "in_progress"},
            ]
        )
        yield ev.ToolCall(
            name="shell",
            display_str="shell: pytest -q",
            icon="⚡",
            args={"command": "pytest -q"},
            call_id="call-1",
        )
        yield ev.ToolResult(
            preview="28 passed",
            full_output=TOOL_OUTPUT,
            call_id="call-1",
        )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        yield ev.InterruptRequest(
            kind="tool",
            payload={
                "action_requests": [
                    {
                        "name": "shell",
                        "description": "pytest -q in the project root",
                        "args": {"command": "pytest -q"},
                    }
                ]
            },
            future=future,
        )
        await future
        yield ev.UsageUpdate(
            input_tokens=1234,
            output_tokens=321,
            session_tokens=1555,
            context_reported=True,
        )
        yield ev.ContextUsage(input_tokens=1234)
        yield ev.AssistantMessage(CLOSING_TEXT, "Nova", "cyan")
        yield ev.Done()

    return stream


async def _wait_for(
    frames: list[dict[str, Any]], kind: str, *, count: int = 1, deadline: float = 10.0
) -> dict:
    """Wait until *count* frames of type *kind* have been emitted.

    Args:
        frames: Captured frames so far.
        kind: Frame type to wait for.
        count: How many of them must exist before returning.
        deadline: Seconds to wait before failing.

    Returns:
        The *count*-th matching frame.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        matching = [frame for frame in frames if frame.get("t") == kind]
        if len(matching) >= count:
            return matching[count - 1]
        await asyncio.sleep(0.01)
    msg = f"no {kind} frame within {deadline}s"
    raise TimeoutError(msg)


# The commands scenario runs the read-only commands the server answers today, so
# the Rust client's command rendering (heading / text / code blocks) is replayed
# against real output instead of hand-written fixtures. Commands whose output
# depends on the machine (log files, dev servers, model settings, workspace
# contents) are deliberately absent: a corpus that changes with the host is not
# a corpus.
# The theme the `theme` scenario selects. Nova's own palette, so the recording
# exercises the extra themes as well as Textual's built-ins.
THEME_NAME = "nova-signal"

COMMANDS: tuple[str, ...] = (
    "help",
    "status",
    "context",
    "ask",
    "learn",
    "images",
    "restore",
    "refine",
    "agents",
    "sessions",
    "skills",
    "mcp",
    "middleware",
    "router",
)


async def _record_commands(host: Any, frames: list[dict[str, Any]]) -> None:  # noqa: ANN401
    """Run each recorded command and wait for its command result.

    Args:
        host: The started SessionHost.
        frames: The captured frame list.
    """
    for index, name in enumerate(COMMANDS, start=1):
        await host.command(f"c{index}", name, None)
        result = await _wait_for(frames, "command_result", count=index)
        if not result.get("ok"):
            msg = f"/{name} answered ok=false in the recording: {result.get('message')}"
            raise RuntimeError(msg)


async def _record_screens(host: Any, frames: list[dict[str, Any]]) -> None:  # noqa: ANN401
    """Ask for every screen kind and answer it, so the frames show the round trip.

    Args:
        host: The started `SessionHost`.
        frames: The captured frame list.
    """
    from novacode_cli.ui.port import ScreenSpec

    for kind, title, payload, answer in SCREENS:
        seen = len([frame for frame in frames if frame.get("t") == "screen"])
        asking = asyncio.create_task(host._ask_screen(ScreenSpec(kind, payload, title)))
        screen = await _wait_for(frames, "screen", count=seen + 1)
        await host.resolve_screen(str(screen["id"]), answer)
        received = await asking
        if received != answer:
            msg = f"the {kind} screen did not round-trip its answer: {received!r}"
            raise RuntimeError(msg)


async def _record_tabs() -> list[dict[str, Any]]:
    """Two sessions, one attach round trip, and their separate transcripts."""
    from novacode_cli import ui_events as ev
    from novacode_cli.server import frames as wire
    from novacode_cli.server.host import SessionHost, SessionSpec
    from novacode_cli.server.sessions import SessionRegistry

    registry = SessionRegistry()
    state_a = _SessionState(session_id="session-alpha")
    state_b = _SessionState(session_id="session-beta")
    spec_a = SessionSpec(
        agent=object(),
        session_state=state_a,
        assistant_id="nova-agent",
        model_name=MODEL_NAME,
        cwd="C:/work/alpha",
        name="alpha",
        session_id="session-alpha",
    )
    spec_b = SessionSpec(
        agent=object(),
        session_state=state_b,
        assistant_id="nova-agent",
        model_name=MODEL_NAME,
        cwd="C:/work/beta",
        name="beta",
        session_id="session-beta",
    )
    stream_a: list[dict[str, Any]] = []
    stream_b: list[dict[str, Any]] = []
    host_a = SessionHost(stream_a.append, lambda: spec_a, registry=registry)
    host_b = SessionHost(stream_b.append, lambda: spec_b, registry=registry)
    await host_a.start()
    await host_b.start()
    # A client sees only its own `ready`; the transport emits `notice` then
    # `sessions` after each real attach, which is what these frames are.
    out: list[dict[str, Any]] = [frame for frame in stream_a if frame.get("t") == "ready"]
    out.append(wire.sessions_frame(registry.summary(), active="session-alpha"))
    out.append(wire.event_frame(ev.AssistantMessage("Alpha one", "Nova", "cyan")))
    out.append(wire.notice("Attached to beta."))
    out.append(wire.sessions_frame(registry.summary(), active="session-beta"))
    out.append(wire.event_frame(ev.AssistantMessage("Beta one", "Nova", "cyan")))
    out.append(wire.notice("Attached to alpha."))
    out.append(wire.sessions_frame(registry.summary(), active="session-alpha"))
    out.append(wire.event_frame(ev.AssistantMessage("Alpha two", "Nova", "cyan")))
    await host_a.close()
    await host_b.close()
    return out


async def _record_council() -> list[dict[str, Any]]:
    """A deterministic council run: progress events, then the plans screen."""
    from novacode_cli import council_planner as cp
    from novacode_cli import ui_events as ev
    from novacode_cli.server import frames as wire
    from novacode_cli.ui import screen_specs

    run = cp.CouncilRun(id="council-1", prompt="Add retry logic")
    run.status = cp.AWAITING_APPROVAL
    run.proposals = [
        cp.Proposal(
            id="p1",
            agent_id="a1",
            title="Exponential backoff",
            summary="Retry with bounded exponential backoff.",
            implementation_steps=["Wrap the call", "Add a test"],
        ),
        cp.Proposal(
            id="p2",
            agent_id="a2",
            title="Fixed retry",
            summary="Retry a fixed number of times.",
            implementation_steps=["Wrap the call"],
        ),
    ]
    run.judgment = cp.Judgment(
        selected_plans=[cp.SelectedPlan("p1", 1, 9.0), cp.SelectedPlan("p2", 2, 7.0)]
    )
    agents = [
        {"id": "architect", "name": "Architect", "avatar": "A", "color": "cyan"},
        {"id": "critic", "name": "Critic", "avatar": "C", "color": "red"},
    ]
    events = [
        {"event": "council.started", "payload": {"agents": agents}, "proposals": 0},
        {"event": "council.phase.started", "payload": {"phase": "brainstorming"}, "proposals": 0},
        {
            "event": "agent.completed",
            "payload": {"agent_id": "architect", "phase": "brainstorming"},
            "proposals": 0,
        },
        {
            "event": "agent.completed",
            "payload": {"agent_id": "critic", "phase": "brainstorming"},
            "proposals": 0,
        },
        {"event": "proposal.created", "payload": {}, "proposals": 1},
        {"event": "proposal.created", "payload": {}, "proposals": 2},
        {"event": "council.phase.started", "payload": {"phase": "critiquing"}, "proposals": 2},
        {
            "event": "proposal.critiqued",
            "payload": {"critic_id": "architect"},
            "proposals": 2,
            "critiques": 1,
        },
        {"event": "council.phase.started", "payload": {"phase": "voting"}, "proposals": 2},
        {
            "event": "vote.submitted",
            "payload": {"cast": 1},
            "proposals": 2,
            "critiques": 1,
            "ballots": 1,
        },
        {
            "event": "vote.submitted",
            "payload": {"cast": 2},
            "proposals": 2,
            "critiques": 1,
            "ballots": 2,
        },
        {
            "event": "vote.tallied",
            "payload": {"tally": {"entropy": 0.4}},
            "proposals": 2,
            "critiques": 1,
            "ballots": 2,
            "entropy": 0.4,
        },
        {"event": "council.phase.started", "payload": {"phase": "judging"}, "proposals": 2},
        {
            "event": "plans.selected",
            "payload": {},
            "proposals": 2,
            "critiques": 1,
            "ballots": 2,
            "entropy": 0.4,
            "outcome": "done",
        },
    ]
    frames: list[dict[str, Any]] = []
    for event in events:
        update = ev.CouncilUpdate(
            prompt=run.prompt,
            run_id=run.id,
            event=str(event["event"]),
            phase=str((event.get("payload") or {}).get("phase") or ""),
            agent_id=str((event.get("payload") or {}).get("agent_id") or ""),
            proposals=int(event.get("proposals") or 0),
            critiques=int(event.get("critiques") or 0),
            ballots=int(event.get("ballots") or 0),
            entropy=event.get("entropy"),
            agents=agents if event["event"] == "council.started" else [],
            outcome=str(event.get("outcome") or ""),
        )
        frame = wire.event_frame(update)
        if frame is not None:
            frames.append(frame)
    spec = screen_specs.council_plans_spec(run)
    frames.append(wire.screen(run.id, kind=spec.kind, title=spec.title, payload=spec.payload))
    return frames


async def record(scenario: str = "core_chat") -> list[dict[str, Any]]:
    """Drive one scripted session and return every frame it emitted.

    Args:
        scenario: `core_chat` (one full turn plus a command), `screens` (the
            screen vocabulary, one modal per kind), `commands` (every served
            command), `theme` (a palette selection and its frame), `tabs`
            (two sessions with an attach round trip) or `council` (progress
            events and the plans screen).

    Returns:
        Frames in emission order, ready to be written as JSONL.
    """
    import novacode_cli.sessions.worker as worker_mod
    from novacode_cli.server.host import SessionHost, SessionSpec

    worker_mod.iterate_agent_events = _scripted_stream()

    if scenario == "tabs":
        return await _record_tabs()
    if scenario == "council":
        return await _record_council()

    frames: list[dict[str, Any]] = []
    spec = SessionSpec(
        agent=object(),
        session_state=_SessionState(tracker=scenario == "commands"),
        assistant_id="nova-agent",
        model_name=MODEL_NAME,
        cwd=CWD,
        session_id=SESSION_ID,
    )
    host = SessionHost(frames.append, lambda: spec)
    await host.start()

    if scenario == "screens":
        await host.send_catalogue()
        await _wait_for(frames, "catalogue")
        await _record_screens(host, frames)
        await host.close()
        return frames

    if scenario == "commands":
        await _record_commands(host, frames)
        await host.close()
        return frames

    if scenario == "theme":
        # An explicit theme name needs no modal: the port applies it, emits the
        # palette for the client, then answers the command. Recording must not
        # touch the user's config, so both config hooks are pinned first.
        from novacode_cli.ui import port_server

        port_server._persisted_theme = lambda: None
        port_server._persist_theme = lambda _name: None
        await host.command("c1", "theme", THEME_NAME)
        await _wait_for(frames, "command_result")
        await host.close()
        return frames

    await host.prompt("p1", PROMPT)
    interrupt = await _wait_for(frames, "interrupt")
    await host.interrupt_reply(
        str(interrupt["id"]), {"decisions": ["approve"], "any_rejected": False}
    )
    await _wait_for(frames, "turn_done")

    await host.command("c1", "status", None)
    await _wait_for(frames, "command_result")
    await host.close()
    return frames


def write_frames(frames: list[dict[str, Any]], out: Path) -> int:
    """Write frames as JSONL, one object per line.

    Args:
        frames: Frames to write.
        out: Destination file; parent directories are created.

    Returns:
        The number of frames written.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        for frame in frames:
            handle.write(json.dumps(frame, ensure_ascii=False, sort_keys=True) + "\n")
    return len(frames)


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("core_chat", "tabs", "council", "screens", "commands", "theme"),
        default="core_chat",
        help="Which scripted session to record",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Destination JSONL file (defaults to tests/fixtures/<scenario>.jsonl)",
    )
    args = parser.parse_args(argv)
    out = args.out or FIXTURES / f"{args.scenario}.jsonl"
    frames = asyncio.run(record(args.scenario))
    count = write_frames(frames, out)
    sys.stdout.write(f"wrote {count} frames to {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
