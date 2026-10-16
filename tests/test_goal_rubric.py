"""`/goal rubric ...` — pair an acceptance rubric with the active goal.

The rubric is threaded into the run as the `rubric` state key (agent_loop), which
deepagents' RubricMiddleware grades against. These tests pin the command surface.
"""

from __future__ import annotations

import types
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from novacode_cli.commands.side_commands import handle_goal_command, restore_goal_state
from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.states.Session import SessionState

if TYPE_CHECKING:
    from pathlib import Path


def _session() -> types.SimpleNamespace:
    return types.SimpleNamespace(active_goal=None, active_rubric=None)


def test_set_goal_then_rubric() -> None:
    ss = _session()
    r1 = handle_goal_command(ss, "build a CSV parser")
    assert r1.action == "set"
    assert ss.active_goal == "build a CSV parser"
    assert ss.active_rubric is None  # setting a goal doesn't touch the rubric

    r2 = handle_goal_command(ss, "rubric - all tests pass\n- handles empty input")
    assert r2.action == "rubric-set"
    assert ss.active_rubric == "- all tests pass\n- handles empty input"
    assert r2.rubric == ss.active_rubric


def test_status_shows_goal_and_rubric() -> None:
    ss = _session()
    handle_goal_command(ss, "do the thing")
    handle_goal_command(ss, "rubric must lint clean")
    r = handle_goal_command(ss, "status")
    assert "Goal (active" in r.message
    assert "Rubric" in r.message


def test_rubric_clear_keeps_goal() -> None:
    ss = _session()
    handle_goal_command(ss, "ship feature")
    handle_goal_command(ss, "rubric green CI")
    handle_goal_command(ss, "rubric clear")
    assert ss.active_rubric is None
    assert ss.active_goal == "ship feature"  # only the rubric was cleared


def test_clear_drops_both_goal_and_rubric() -> None:
    ss = _session()
    handle_goal_command(ss, "ship feature")
    handle_goal_command(ss, "rubric green CI")
    handle_goal_command(ss, "clear")
    assert ss.active_goal is None
    assert ss.active_rubric is None


def test_empty_rubric_arg_clears() -> None:
    ss = _session()
    handle_goal_command(ss, "rubric something")
    assert ss.active_rubric == "something"
    handle_goal_command(ss, "rubric")  # bare `rubric` with no criteria clears it
    assert ss.active_rubric is None


def test_goal_lifecycle_and_budget() -> None:
    ss = _session()
    ss.thread_id = "thread-1"
    handle_goal_command(ss, "Fix parser, verified by its tests")
    assert ss.goal_thread_id == "thread-1"
    assert handle_goal_command(ss, "").action == "status"
    assert handle_goal_command(ss, "pause").action == "pause"
    assert ss.goal_status == "paused"
    assert handle_goal_command(ss, "resume").kickoff
    assert ss.goal_status == "active"
    assert handle_goal_command(ss, "budget 8").action == "budget"
    assert ss.goal_max_turns == 8
    assert handle_goal_command(ss, "budget zero").action == "usage"
    handle_goal_command(ss, "clear")
    assert ss.goal_status is None
    assert ss.goal_thread_id is None


def test_budget_extension_requires_explicit_resume() -> None:
    ss = _session()
    handle_goal_command(ss, "Fix parser")
    ss.goal_status = "budget_limited"
    ss.goal_turns_used = 5
    assert handle_goal_command(ss, "resume").action == "usage"
    handle_goal_command(ss, "budget 7")
    assert ss.goal_status == "paused"
    assert handle_goal_command(ss, "resume").action == "resume"


def test_goal_metadata_round_trips_with_session(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    goal = {
        "objective": "Fix parser",
        "rubric": "pytest passes",
        "status": "paused",
        "turns_used": 2,
        "max_turns": 6,
    }
    manager.save_session(
        "session-1", "thread-1", [HumanMessage(content="start")], "nova", goal=goal
    )
    meta = manager.load_session_meta("session-1")
    assert meta is not None
    assert meta.goal == goal
    assert meta.thread_id == "thread-1"
    restored = SessionState()
    restored.thread_id = meta.thread_id
    restore_goal_state(restored, meta.goal)
    assert restored.active_goal == "Fix parser"
    assert restored.goal_status == "paused"
    assert restored.goal_turns_used == 2
    assert restored.goal_thread_id == "thread-1"


def test_malformed_saved_goal_is_ignored() -> None:
    ss = SessionState()
    restore_goal_state(ss, {"objective": "work", "turns_used": "invalid"})
    assert ss.active_goal is None


def test_new_conversation_drops_thread_goal() -> None:
    ss = SessionState()
    handle_goal_command(ss, "Fix parser")
    previous_thread = ss.thread_id
    ss.reset_conversation()
    assert ss.thread_id != previous_thread
    assert ss.active_goal is None
    assert ss.goal_status is None
    assert ss.goal_thread_id is None
