"""/codex-plan: Codex plans headless (read-only sandbox); Nova approves and implements."""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

import pytest

from novacode_cli.commands import codex_plan
from novacode_cli.commands.claude_plan import PlanTurn
from novacode_cli.commands.codex_plan import run_turn

# A stand-in for `codex exec --json`, events as recorded from codex-cli 0.163.
FAKE_CODEX = r'''
import json, sys
args = sys.argv[1:]
def emit(event): print(json.dumps(event), flush=True)
assert 'sandbox_mode="read-only"' in args, args
resumed = args[:2] == ["exec", "resume"]
emit({"type": "thread.started", "thread_id": "t-1"})
emit({"type": "turn.started"})
cmd = '"C:\\\\Program Files\\\\PowerShell\\\\7\\\\pwsh.exe" -Command \'Get-Content -LiteralPath app.py\''
emit({"type": "item.started", "item": {"type": "command_execution", "command": cmd}})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Looking around."}})
if not resumed:
    assert "Task:" in args[-1], "the first turn carries the planning instructions"
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "1. Which database?"}})
else:
    assert args[2] == "t-1", "resumes the same thread"
    emit({"type": "item.completed", "item": {"type": "agent_message",
          "text": "# Plan\n\n1. Add the Postgres store.\n\nPLAN_READY"}})
emit({"type": "turn.completed", "usage": {}})
'''


@pytest.fixture
def fake_codex(monkeypatch):
    script = pathlib.Path(tempfile.mkdtemp()) / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    real_exec = asyncio.create_subprocess_exec

    async def spawn(exe, *args, **kwargs):
        return await real_exec(sys.executable, str(script), *args, **kwargs)

    monkeypatch.setattr(codex_plan, "executable", lambda: "codex")
    monkeypatch.setattr(codex_plan.asyncio, "create_subprocess_exec", spawn)


@pytest.mark.asyncio
async def test_a_question_then_the_plan_in_the_final_message(fake_codex):
    steps: list[str] = []
    first = await run_turn("Plan auth", pathlib.Path.cwd(), on_step=steps.append)
    assert first.error is None and first.session_id == "t-1"
    assert first.plan is None and first.text == "1. Which database?", "the last message is the reply"
    assert steps == ["$ Get-Content -LiteralPath app.py"], "the shell wrapper is stripped"

    second = await run_turn("Postgres", pathlib.Path.cwd(), session_id="t-1")
    assert second.plan == "# Plan\n\n1. Add the Postgres store." == second.text


def test_a_login_failure_is_recognised():
    assert codex_plan.needs_login("Not logged in. Please run codex login")
    assert not codex_plan.needs_login("model overloaded")


@pytest.mark.asyncio
async def test_a_failed_turn_is_reported(monkeypatch):
    script = pathlib.Path(tempfile.mkdtemp()) / "fail.py"
    script.write_text(
        "import json\nprint(json.dumps({'type': 'turn.failed', 'error': {'message': 'model overloaded'}}))\n",
        encoding="utf-8",
    )
    real_exec = asyncio.create_subprocess_exec

    async def spawn(exe, *args, **kwargs):
        return await real_exec(sys.executable, str(script), *args, **kwargs)

    monkeypatch.setattr(codex_plan, "executable", lambda: "codex")
    monkeypatch.setattr(codex_plan.asyncio, "create_subprocess_exec", spawn)
    turn = await run_turn("x", pathlib.Path.cwd())
    assert turn.error == "model overloaded"


@pytest.mark.asyncio
async def test_the_command_hands_codexs_approved_plan_to_nova(monkeypatch):
    from tests.test_tui_compaction_row import _app

    plan = "# v1\n\n## Steps\n### 1. Add auth"
    turns = iter([
        PlanTurn(session_id="t-1", text="Which database?"),
        PlanTurn(session_id="t-1", text="ok", plan=plan),
    ])
    prompts: list[tuple[str, str | None]] = []

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, **_):
        prompts.append((prompt, session_id))
        return next(turns)

    monkeypatch.setattr(codex_plan, "run_turn", fake_turn)
    monkeypatch.setattr(codex_plan, "executable", lambda: "codex")
    app = _app()
    stored: list[str] = []
    app.session_state.set_approved_plan = stored.append
    ran: list[str] = []

    async def dock(payload):
        assert payload["context"] == "Codex · planning"
        return {"response": {"answer": "Postgres", "selected_index": None}}

    async def approve(screen):
        return "auto"

    app.session_state.consume_approved_plan = lambda: stored.pop() if stored else None
    app.session_state.clear_plan_agent = lambda: None
    seeded: list[tuple[dict, dict]] = []

    async def update_state(*, config, values, as_node=None):
        seeded.append((config, values))

    async def stream_prompt(prompt):
        ran.append(prompt)

    app.agent.aupdate_state = update_state
    app._stream_prompt = stream_prompt
    app._paint_todos = lambda _todos: None

    async with app.run_test(size=(120, 40)):
        app.session_state.auto_approve = False
        monkeypatch.setattr(app, "_show_question_dock", dock)
        monkeypatch.setattr(app, "push_screen_wait", approve)
        await app._run_codex_plan("/codex-plan add auth")

    assert prompts[1] == ("Postgres", "t-1")
    assert len(ran) == 1 and plan in ran[0]
    assert seeded[0][1]["todos"] == [{"content": "Add auth", "status": "in_progress"}]
    assert seeded[0][0]["configurable"]["thread_id"] == app.session_state.thread_id
    assert not app.session_state.auto_approve and not app._plan_scoped_auto_approve


@pytest.mark.asyncio
async def test_missing_codex_offers_the_install(monkeypatch):
    from tests.test_tui_compaction_row import _app

    monkeypatch.setattr(codex_plan, "executable", lambda: None)
    app = _app()
    titles: list[str] = []
    installs: list[bool] = []

    async def choose(screen):
        titles.append(screen._title)
        return 0

    async def fake_install(on_line):
        installs.append(True)
        return True

    monkeypatch.setattr(codex_plan, "install", fake_install)
    logs: list[str] = []
    async with app.run_test(size=(120, 40)):
        monkeypatch.setattr(app, "push_screen_wait", choose)
        monkeypatch.setattr(app, "_log", lambda t, *a, **k: logs.append(str(t)))
        await app._run_codex_plan("/codex-plan add auth")
    assert titles == ["Codex is not installed"] and installs
    assert any("codex login" in line for line in logs)


@pytest.mark.asyncio
async def test_novas_openai_settings_do_not_reach_codex(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-nova")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9931/v1")
    envs: list[dict] = []

    async def fake_run(prompt, cwd, *, session_id, on_step, env, **_):
        envs.append(env)
        return PlanTurn(session_id="t", text="ok")

    monkeypatch.setattr(codex_plan, "_run", fake_run)
    await run_turn("x", pathlib.Path.cwd())
    assert "OPENAI_API_KEY" not in envs[0] and "OPENAI_BASE_URL" not in envs[0]
