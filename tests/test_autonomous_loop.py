"""Autonomous goal mode — the ``run_with_goal`` wrapper + shared helpers.

Covers marker detection (``goal_achieved``), the between-turn continuation
prompt (``build_goal_followup``), and the wrapper's pass / continue / exhaust /
clear / error-passthrough behaviour over a faked ``iterate_agent_events``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import novacode_cli.core.autonomous_loop as al
from novacode_cli import ui_events as ev
from novacode_cli.commands.side_commands import (
    DEFAULT_GOAL_MAX_TURNS,
    build_goal_followup,
    goal_achieved,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


def _msg(text: str) -> ev.AssistantMessage:
    return ev.AssistantMessage(text=text, agent_name="Nova", agent_color="cyan")


def _fake_iterate(
    scripts: list[list],
    side_effect: Callable[[str, Any], None] | None = None,
) -> tuple[Any, dict]:
    """Return a stand-in for ``iterate_agent_events`` + a call recorder.

    ``scripts[i]`` is the event list the i-th invocation yields. ``side_effect``
    (optional) is called with ``(user_input, session_state)`` before each run.
    """
    calls: dict = {"n": 0, "inputs": []}

    def _factory(
        user_input: str,
        _agent: Any,  # noqa: ANN401 — framework object, matches agent_loop's untyped agent
        _assistant_id: str | None,
        session_state: Any,  # noqa: ANN401 — duck-typed session shim
        **_kwargs: Any,
    ) -> AsyncIterator[Any]:
        if side_effect is not None:
            side_effect(user_input, session_state)
        idx = calls["n"]
        calls["n"] += 1
        calls["inputs"].append(user_input)
        events = scripts[idx] if idx < len(scripts) else [ev.Done()]

        async def _gen() -> AsyncIterator[Any]:
            for e in events:
                yield e

        return _gen()

    return _factory, calls


def _session(
    goal: str | None = "do the thing", max_turns: int = DEFAULT_GOAL_MAX_TURNS
) -> SimpleNamespace:
    return SimpleNamespace(
        thread_id="t1",
        active_goal=goal,
        goal_status="active" if goal else None,
        goal_thread_id="t1" if goal else None,
        goal_turns_used=0,
        goal_max_turns=max_turns,
    )


async def _collect(gen: Any) -> list:  # noqa: ANN401 — async generator stand-in
    return [e async for e in gen]


# ── marker detection ─────────────────────────────────────────────────────────


class TestGoalAchieved:
    def test_marker_present(self):
        assert goal_achieved("**GOAL ACHIEVED**\nEvidence: tests passed.")

    def test_marker_absent(self):
        assert not goal_achieved("Still working on it.")

    def test_case_insensitive(self):
        assert goal_achieved("goal achieved\nevidence: tests passed")
        assert goal_achieved("Goal Achieved\nEvidence: artifact exists")

    def test_marker_in_multiline_text(self):
        assert goal_achieved("step 1 done\nGOAL ACHIEVED\nEvidence: step 2 test passed")
        assert not goal_achieved("Quoted: GOAL ACHIEVED\nEvidence: none")
        assert not goal_achieved("GOAL ACHIEVED\nsummary without evidence")

    def test_empty_text(self):
        assert not goal_achieved("")

    def test_completion_example_inside_code_fence_is_not_completion(self):
        assert not goal_achieved("Example:\n```text\nGOAL ACHIEVED\nEvidence: example\n```")


# ── continuation prompt ──────────────────────────────────────────────────────


class TestBuildGoalFollowup:
    def test_mentions_goal_turn_and_remaining(self):
        p = build_goal_followup("build a CSV parser", 2, 5)
        assert "build a CSV parser" in p
        assert "Turn 2/5" in p
        assert "3 turn(s) remaining" in p

    def test_remaining_clamped_at_zero(self):
        p = build_goal_followup("g", 5, 5)
        assert "0 turn(s) remaining" in p

    def test_reminds_agent_of_marker(self):
        p = build_goal_followup("g", 1, 3)
        assert "GOAL ACHIEVED" in p


# ── run_with_goal wrapper ────────────────────────────────────────────────────


class TestRunWithGoal:
    async def test_passthrough_when_no_goal(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate([[_msg("answer"), ev.Done(had_response=True)]])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("task", None, None, _session(goal=None)))
        assert calls["n"] == 1
        assert calls["inputs"] == ["task"]
        assert sum(isinstance(e, ev.Done) for e in out) == 1
        assert not any(
            isinstance(e, ev.ContextMessage) and e.event_type == "nova_goal_continue" for e in out
        )

    async def test_achieved_on_first_turn_single_done(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate(
            [[_msg("**GOAL ACHIEVED**\nEvidence: tests passed"), ev.Done(had_response=True)]]
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("kickoff", None, None, _session()))
        assert calls["n"] == 1
        assert sum(isinstance(e, ev.Done) for e in out) == 1
        # Single-turn run: no continuation, no finish notice.
        assert not any(
            isinstance(e, ev.ContextMessage) and e.event_type.startswith("nova_goal_") for e in out
        )

    async def test_not_achieved_continues_with_followup(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate(
            [
                [_msg("progress so far"), ev.Done(had_response=True)],
                [_msg("**GOAL ACHIEVED**\nEvidence: tests passed"), ev.Done(had_response=True)],
            ]
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("kickoff", None, None, _session()))
        assert calls["n"] == 2
        # Second turn is re-driven with the continuation prompt.
        assert calls["inputs"][1].startswith("[GOAL CONTINUATION] Turn 1/5")
        assert "do the thing" in calls["inputs"][1]
        # One continue notice, one finish notice, one (final) Done.
        cont = [e for e in out if getattr(e, "event_type", "") == "nova_goal_continue"]
        fin = [e for e in out if getattr(e, "event_type", "") == "nova_goal_finish"]
        assert len(cont) == 1
        assert len(fin) == 1
        assert "finished after 2 turn(s)" in fin[0].message
        assert sum(isinstance(e, ev.Done) for e in out) == 1

    async def test_max_turns_cap_stops_run(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate(
            [
                [_msg("turn 1"), ev.Done(had_response=True)],
                [
                    ev.ToolCall(name="test", display_str="test", icon="•"),
                    _msg("turn 2"),
                    ev.Done(had_response=True),
                ],
                [_msg("turn 3"), ev.Done(had_response=True)],
            ]
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("kickoff", None, None, _session(max_turns=3)))
        assert calls["n"] == 3
        fin = [e for e in out if getattr(e, "event_type", "") == "nova_goal_finish"]
        assert len(fin) == 1
        assert "budget reached after 3/3 turn(s)" in fin[0].message
        assert sum(isinstance(e, ev.Done) for e in out) == 1

    async def test_goal_cleared_mid_run_stops(self, monkeypatch: pytest.MonkeyPatch):
        def _clear_after_first(
            _user_input: str,
            session_state: Any,  # noqa: ANN401 — duck-typed session shim
        ) -> None:
            if session_state._cleared:
                return
            session_state._cleared = True
            session_state.active_goal = None

        factory, calls = _fake_iterate(
            [
                [_msg("turn 1"), ev.Done(had_response=True)],
                [_msg("turn 2"), ev.Done(had_response=True)],
            ],
            side_effect=_clear_after_first,
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        ss = _session()
        ss._cleared = False
        out = await _collect(al.run_with_goal("kickoff", None, None, ss))
        # The goal was cleared after turn 1 -> no second agent run.
        assert calls["n"] == 1
        fin = [e for e in out if getattr(e, "event_type", "") == "nova_goal_finish"]
        assert len(fin) == 1
        assert "goal cleared" in fin[0].message
        assert sum(isinstance(e, ev.Done) for e in out) == 1

    async def test_error_passes_through(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate([[_msg("partial"), ev.Error("boom")]])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("kickoff", None, None, _session()))
        assert any(isinstance(e, ev.Error) for e in out)
        assert not any(isinstance(e, ev.Done) for e in out)
        assert calls["n"] == 1

    async def test_cancelled_passes_through(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate([[_msg("partial"), ev.Cancelled()]])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("kickoff", None, None, _session()))
        assert any(isinstance(e, ev.Cancelled) for e in out)
        assert not any(isinstance(e, ev.Done) for e in out)
        assert calls["n"] == 1

    async def test_pause_between_turns_and_resumption_keeps_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ss = _session(max_turns=3)

        def pause(_input: str, session_state: Any) -> None:  # noqa: ANN401
            session_state.goal_status = "paused"

        factory, calls = _fake_iterate([[_msg("progress"), ev.Done()]], side_effect=pause)
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        await _collect(al.run_with_goal("kickoff", None, None, ss))
        assert calls["n"] == 1
        assert ss.goal_status == "paused"
        assert ss.goal_turns_used == 1

        ss.goal_status = "active"
        factory, calls = _fake_iterate([[_msg("GOAL ACHIEVED\nEvidence: test passed"), ev.Done()]])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        await _collect(al.run_with_goal("resume", None, None, ss))
        assert calls["n"] == 1
        assert ss.goal_turns_used == 2
        assert ss.goal_status == "complete"

    async def test_goal_does_not_run_in_another_thread(self, monkeypatch: pytest.MonkeyPatch):
        ss = _session()
        ss.goal_thread_id = "original"
        factory, calls = _fake_iterate([[_msg("ordinary reply"), ev.Done()]])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        await _collect(al.run_with_goal("hello", None, None, ss))
        assert calls["inputs"] == ["hello"]
        assert ss.active_goal == "do the thing"

    async def test_tool_free_continuation_does_not_spin(self, monkeypatch: pytest.MonkeyPatch):
        factory, calls = _fake_iterate(
            [[_msg("first"), ev.Done()], [_msg("second"), ev.Done()], [_msg("third"), ev.Done()]]
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        ss = _session(max_turns=8)
        out = await _collect(al.run_with_goal("kickoff", None, None, ss))
        assert calls["n"] == 2
        assert ss.goal_status == "active"
        assert ss.goal_turns_used == 2
        assert any(
            "without tool work" in e.message for e in out if isinstance(e, ev.ContextMessage)
        )

    async def test_plan_mode_runs_once_without_spending_goal_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ss = _session()
        ss.plan_mode_enabled = True
        factory, calls = _fake_iterate([[_msg("Here is the plan"), ev.Done()]])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        await _collect(al.run_with_goal("plan first", None, None, ss))
        assert calls["n"] == 1
        assert ss.goal_turns_used == 0

    async def test_exhausted_budget_still_emits_done(self, monkeypatch: pytest.MonkeyPatch):
        ss = _session(max_turns=2)
        ss.goal_turns_used = 2
        factory, calls = _fake_iterate([])
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("continue", None, None, ss))
        assert calls["n"] == 0
        assert ss.goal_status == "budget_limited"
        assert isinstance(out[-1], ev.Done)

    async def test_task_cancellation_pauses_goal(self, monkeypatch: pytest.MonkeyPatch):
        ss = _session()

        async def cancelled(*_args: object, **_kwargs: object) -> AsyncIterator[Any]:
            yield _msg("working")
            raise asyncio.CancelledError

        monkeypatch.setattr(al, "iterate_agent_events", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await _collect(al.run_with_goal("start", None, None, ss))
        assert ss.goal_status == "paused"

    async def test_queued_input_runs_before_next_goal_continuation(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ss = _session()
        factory, calls = _fake_iterate(
            [[_msg("GOAL ACHIEVED\nEvidence: old requirements passed"), ev.Done()]]
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        out = await _collect(al.run_with_goal("start", None, None, ss, can_continue=lambda: False))
        assert calls["n"] == 1
        assert ss.goal_status == "active"
        assert isinstance(out[-1], ev.Done)

    async def test_replacing_goal_does_not_accept_old_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from novacode_cli.commands.side_commands import handle_goal_command

        ss = _session()
        replacements = []

        def replace_once(_input: str, state: SimpleNamespace) -> None:
            if not replacements:
                handle_goal_command(state, "new objective")
                state.goal_max_turns = 1
                replacements.append(True)

        factory, calls = _fake_iterate(
            [
                [_msg("GOAL ACHIEVED\nEvidence: old test passed"), ev.Done()],
                [_msg("new work still needed"), ev.Done()],
            ],
            side_effect=replace_once,
        )
        monkeypatch.setattr(al, "iterate_agent_events", factory)
        await _collect(al.run_with_goal("start old objective", None, None, ss))
        assert calls["n"] == 2
        assert "new objective" in calls["inputs"][1]
        assert ss.active_goal == "new objective"
        assert ss.goal_status == "budget_limited"
        assert ss.goal_turns_used == 1
