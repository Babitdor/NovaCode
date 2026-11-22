"""`/goal` over the socket: the shared parser, with `kickoff` as `prompt`.

The state machine lives in `novacode_cli/commands/side_commands.py`, shared with the
Textual UI and the REPL. The port only adapts its `GoalCommandResult`: the message
becomes blocks, and `kickoff` (the prompt that starts autonomous execution) rides
the protocol's `prompt` field, so the host starts the turn as `/init` does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui.port_server import ServerPort


class _SS:
    """Session state with the goal fields the shared helper reads."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.todos: list[Any] = []
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None
        self.active_goal: str | None = None
        self.active_rubric: str | None = None
        self.goal_status: str | None = None
        self.goal_turns_used = 0
        self.goal_max_turns = 10
        self.goal_thread_id: str | None = None
        self.goal_revision: str | None = None


def _port(state: _SS | None = None) -> ServerPort:
    """A port over a worker whose session state carries the goal fields."""
    worker = SimpleNamespace(
        agent=object(),
        session_state=state or _SS(),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )
    sent: list[dict[str, Any]] = []
    return ServerPort(sent.append, worker)


async def test_setting_a_goal_returns_its_kickoff_as_the_prompt() -> None:
    state = _SS()
    port = _port(state)
    result = await port.run("/goal", "ship the port")
    assert result.ok is True
    assert state.active_goal == "ship the port"
    assert state.goal_status == "active"
    assert result.prompt, "a set goal must ask the host to start the run"
    assert "ship the port" in result.prompt
    assert "Goal set" in result.blocks[0].text


async def test_status_reports_the_goal_and_the_turn_budget() -> None:
    state = _SS()
    state.active_goal = "ship the port"
    state.goal_status = "active"
    state.goal_turns_used = 2
    port = _port(state)
    result = await port.run("/goal", None)
    assert result.ok is True
    assert "ship the port" in result.blocks[0].text
    assert "2/10 turns" in result.blocks[0].text
    assert result.prompt is None, "a status query must not start a run"


async def test_pause_and_resume_move_the_state() -> None:
    state = _SS()
    port = _port(state)
    await port.run("/goal", "ship the port")
    paused = await port.run("/goal", "pause")
    assert state.goal_status == "paused"
    assert paused.prompt is None
    resumed = await port.run("/goal", "resume")
    assert state.goal_status == "active"
    assert resumed.prompt, "resuming continues the run"


async def test_clear_drops_the_goal() -> None:
    state = _SS()
    port = _port(state)
    await port.run("/goal", "ship the port")
    result = await port.run("/goal", "clear")
    assert result.ok is True
    assert state.active_goal is None


async def test_a_bad_budget_is_a_usage_answer() -> None:
    port = _port()
    result = await port.run("/goal", "budget lots")
    assert result.ok is False
    assert "Usage: /goal budget" in result.message


async def test_without_a_session_it_says_so() -> None:
    worker = SimpleNamespace(
        agent=object(),
        session_state=None,
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )
    port = ServerPort(list.append.__get__([]), worker)
    result = await port.run("/goal", "ship it")
    assert result.ok is False
    assert result.message == "Session is unavailable."


@pytest.mark.parametrize("name", ["auto-approve", "goal", "stop", "verbose"])
def test_each_port_command_is_served(name: str) -> None:
    from novacode_cli.server.catalog import get

    spec = get(name)
    assert spec is not None
    assert (spec.route, spec.served, spec.evidence) == ("passthrough", True, "port-command")
