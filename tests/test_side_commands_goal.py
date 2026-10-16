"""Goal-mode helpers in side_commands: turn cap, achievement marker, follow-up."""

from __future__ import annotations

from novacode_cli.commands.side_commands import (
    DEFAULT_GOAL_MAX_TURNS,
    build_goal_followup,
    goal_achieved,
)


class TestGoalAchieved:
    def test_detects_marker_case_insensitively(self):
        assert goal_achieved("**GOAL ACHIEVED**\nEvidence: tests passed")
        assert goal_achieved("goal achieved\nevidence: tests passed")
        assert goal_achieved("Goal Achieved\nEvidence: diff reviewed")

    def test_returns_false_without_marker(self):
        assert not goal_achieved("Still working on it.")
        assert not goal_achieved("")
        assert not goal_achieved(None)  # type: ignore[arg-type]


class TestBuildGoalFollowup:
    def test_includes_goal_and_turn_budget(self):
        out = build_goal_followup("ship the feature", 3, 8)
        assert "ship the feature" in out
        assert "Turn 3/8" in out
        assert "5 turn(s) remaining" in out
        assert "GOAL ACHIEVED" in out


class TestDefaultTurnCap:
    def test_default_is_positive(self):
        assert isinstance(DEFAULT_GOAL_MAX_TURNS, int)
        assert DEFAULT_GOAL_MAX_TURNS > 0
