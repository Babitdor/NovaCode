"""/claude-review and /codex-review: a headless read-only review, reported by Nova."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

from novacode_cli.commands import claude_plan, codex_plan
from novacode_cli.commands.claude_plan import PlanTurn

# Records its own arguments, then answers with a report.
FAKE_CLI = r'''
import json, sys
print(json.dumps({"type": "system", "subtype": "init", "session_id": "r-1"}), flush=True)
print(json.dumps({"type": "thread.started", "thread_id": "r-1"}), flush=True)
report = "## Findings\n**[high]** `app.py:3` - crash"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": report}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "session_id": "r-1", "result": report}), flush=True)
print(json.dumps({"type": "args", "args": sys.argv[1:]}), file=sys.stderr)
'''


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    seen: list[list[str]] = []
    real_exec = asyncio.create_subprocess_exec

    async def spawn(exe, *args, **kwargs):
        seen.append(list(args))
        return await real_exec(sys.executable, str(script), *args, **kwargs)

    for module in (claude_plan, codex_plan):
        monkeypatch.setattr(module, "executable", lambda: "cli")
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    return seen


@pytest.mark.asyncio
async def test_claude_reviews_without_plan_mode_or_edit_tools(fake_cli):
    turn = await claude_plan.run_turn("focus", pathlib.Path.cwd(), **claude_plan.REVIEW_ARGS)
    args = fake_cli[0]
    assert turn.error is None and "app.py:3" in turn.text and turn.plan is None
    assert "plan" not in args[args.index("--permission-mode") + 1]
    assert "Edit" in args[args.index("--disallowedTools") + 1]
    assert "Bash(git diff:*)" in args[args.index("--allowedTools") + 1]
    assert "You are reviewing code" in args[args.index("--append-system-prompt") + 1]


@pytest.mark.asyncio
async def test_codex_reviews_in_its_read_only_sandbox(fake_cli):
    turn = await codex_plan.run_turn("focus", pathlib.Path.cwd(), **codex_plan.REVIEW_ARGS)
    args = fake_cli[0]
    assert turn.error is None and "app.py:3" in turn.text
    assert 'sandbox_mode="read-only"' in args
    assert args[-1].startswith("You are reviewing code") and args[-1].endswith("focus")


@pytest.mark.asyncio
async def test_the_report_goes_to_nova_to_check_and_report(monkeypatch):
    from tests.test_tui_compaction_row import _app

    calls: list[tuple[str, dict]] = []

    async def fake_turn(prompt, cwd, **kwargs):
        calls.append((prompt, kwargs))
        return PlanTurn(session_id="r-1", text="## Findings\n**[high]** `app.py:3` - crash")

    monkeypatch.setattr(claude_plan, "run_turn", fake_turn)
    monkeypatch.setattr(claude_plan, "executable", lambda: "claude")
    app = _app()
    sent: list[str] = []

    async def stream_prompt(prompt):
        sent.append(prompt)

    app._stream_prompt = stream_prompt
    async with app.run_test(size=(120, 40)):
        await app._run_claude_review("/claude-review the auth module")

    assert calls[0][0] == "the auth module"
    assert calls[0][1]["instructions"] == claude_plan.REVIEW_INSTRUCTIONS
    assert len(sent) == 1 and "Verify each finding" in sent[0]
    assert '<review_report reviewer="Claude Code">' in sent[0] and "app.py:3" in sent[0]
    json.dumps(sent)  # plain text


@pytest.mark.asyncio
async def test_a_stopped_review_resumes_its_session(monkeypatch, tmp_path):
    from novacode_cli.commands import planner_usage as pu
    from tests.test_tui_compaction_row import _app

    monkeypatch.setattr(pu, "_PLANS", tmp_path / "planner_sessions.json")
    calls: list[tuple[str, str | None]] = []

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, on_session=None, **_):
        calls.append((prompt, session_id))
        if len(calls) == 1:
            on_session("r-1")
            raise asyncio.CancelledError  # Esc, or /stop from Telegram
        return PlanTurn(session_id="r-1", text="## Findings\nNone.")

    monkeypatch.setattr(codex_plan, "run_turn", fake_turn)
    monkeypatch.setattr(codex_plan, "executable", lambda: "codex")
    app = _app()
    sent: list[str] = []

    async def stream_prompt(prompt):
        sent.append(prompt)

    app._stream_prompt = stream_prompt
    async with app.run_test(size=(120, 40)):
        with pytest.raises(asyncio.CancelledError):
            await app._run_codex_review("/codex-review the parser")
        await app._run_codex_review("/codex-review resume")
        await app._run_codex_review("/codex-review resume")  # nothing left to resume

    assert calls[1][1] == "r-1" and "the parser" in calls[1][0]
    assert len(calls) == 2 and len(sent) == 1


@pytest.mark.asyncio
async def test_telegram_runs_the_review_and_hears_about_it(monkeypatch):
    from tests.test_tui_compaction_row import _app

    async def fake_turn(prompt, cwd, **kwargs):
        return PlanTurn(session_id="r-1", text="## Findings\nNone.")

    monkeypatch.setattr(claude_plan, "run_turn", fake_turn)
    monkeypatch.setattr(claude_plan, "executable", lambda: "claude")
    app = _app()
    replies: list[str] = []

    async def reply(text):
        replies.append(text)

    class Msg:
        reply_fn = staticmethod(reply)

    async def stream_prompt(prompt):
        pass

    app._stream_prompt = stream_prompt
    async with app.run_test(size=(120, 40)) as pilot:
        reply_text, run = await app._remote_slash("/claude_review the parser")
        assert reply_text is None and callable(run), "Telegram's /claude_review is the command"
        app._remote_msg = Msg()
        await app._run_claude_review("/claude-review the parser")
        await pilot.pause()

    assert any("is reviewing (the parser)" in r for r in replies)
    assert any("finished its review" in r for r in replies)
