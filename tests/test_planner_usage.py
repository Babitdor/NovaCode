"""/claude-plan and /codex-plan: usage readings, and resuming an interrupted planning."""

from __future__ import annotations

import asyncio
import json

import pytest

from novacode_cli.commands import planner_usage as pu
from novacode_cli.commands.claude_plan import PlanTurn

pytestmark = pytest.mark.usefixtures("isolated_plan_storage")


@pytest.fixture(autouse=True)
def stores(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "_STORE", tmp_path / "planner_usage.json")
    monkeypatch.setattr(pu, "_PLANS", tmp_path / "planner_sessions.json")


def test_codex_usage_comes_from_its_newest_session_file(tmp_path):
    day = tmp_path / "sessions" / "2026" / "10" / "17"
    day.mkdir(parents=True)
    limits = {
        "primary": {"used_percent": 54.0, "window_minutes": 300, "resets_at": 4102444800},
        "secondary": {"used_percent": 100.0, "window_minutes": 10080, "resets_at": 4102444800},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    }
    event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
    (day / "rollout-1.jsonl").write_text('{"type":"session_meta"}\n' + json.dumps(event) + "\n", encoding="utf-8")

    usage = pu.codex_usage(tmp_path / "sessions")
    assert [(label, used) for label, used, _ in usage.windows] == [("5-hour", 0.54), ("weekly", 1.0)]
    line = pu.format_usage(usage)
    assert " 54% used" in line and "100% used" in line and "LIMIT REACHED" in line
    bar = pu.usage_text(usage)
    assert any(s.style == "red" for s in bar.spans), "a full window has a red bar"

    assert "No reading yet" in pu.format_usage(pu.codex_usage(tmp_path / "missing"))


def test_claude_usage_is_the_last_rate_limit_event_seen():
    assert "No reading yet" in pu.format_usage(pu.claude_usage())
    pu.record_claude({
        "status": "allowed_warning",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.47, "resetsAt": 4102444800},
            "seven_day": {"utilization": 0.31, "resetsAt": 4102444800},
        },
    })
    line = pu.format_usage(pu.claude_usage())
    assert " 47% used" in line and " 31% used" in line and "nearing a limit" in line


@pytest.mark.asyncio
async def test_an_interrupted_planning_resumes_its_session(monkeypatch):
    import novacode_cli.commands.claude_plan as module
    from novacode_cli.config.config import settings
    from tests.test_tui_compaction_row import _app

    calls: list[tuple[str, str | None]] = []

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, on_session=None):
        calls.append((prompt, session_id))
        if len(calls) == 1:
            on_session("s-1")  # Claude has started, then the user presses Esc
            raise asyncio.CancelledError
        return PlanTurn(session_id="s-1", text="Done.", plan="# Plan")

    monkeypatch.setattr(module, "run_turn", fake_turn)
    monkeypatch.setattr(module, "executable", lambda: "claude")
    app = _app()
    approved: list[str] = []
    app.session_state.set_approved_plan = approved.append

    async def approve(screen):
        return "manual"

    async def run_approved():
        pass

    async with app.run_test(size=(120, 40)):
        monkeypatch.setattr(app, "push_screen_wait", approve)
        monkeypatch.setattr(app, "_maybe_run_approved_plan", run_approved)
        with pytest.raises(asyncio.CancelledError):
            await app._run_claude_plan("/claude-plan add auth")
        root = str(settings.get_workspace_root())
        saved = pu.interrupted("/claude-plan", root)
        assert saved["session"] == "s-1" and saved["task"] == "add auth"

        await app._run_claude_plan("/claude-plan resume")

    assert calls[1][1] == "s-1" and "add auth" in calls[1][0], "resumes the same session and task"
    assert approved == ["# Plan"]
    assert pu.interrupted("/claude-plan", root) is None, "a finished planning is forgotten"


def test_a_window_that_has_reset_since_the_reading_shows_as_reset():
    usage = pu.Usage("Codex", [("5-hour", 0.9, 1)])
    assert "0% used  reset since" in pu.format_usage(usage)


def test_a_half_written_last_line_falls_back_to_the_line_before(tmp_path):
    day = tmp_path / "2026" / "10" / "17"
    day.mkdir(parents=True)
    good = {"rate_limits": {"primary": {"used_percent": 20.0, "resets_at": 4102444800}}}
    (day / "rollout-1.jsonl").write_text(json.dumps(good) + '\n{"rate_limits": {"prim', encoding="utf-8")
    assert pu.codex_usage(tmp_path).windows[0][1] == 0.2
