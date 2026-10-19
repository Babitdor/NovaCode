"""/claude-plan: Claude Code plans headless; Nova approves and implements."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

import pytest

from novacode_cli.commands import claude_plan
from novacode_cli.commands.claude_plan import PlanTurn, run_turn

pytestmark = pytest.mark.usefixtures("isolated_plan_storage")

# A stand-in for `claude -p ... --output-format stream-json`: a question on the
# first turn, a written plan once resumed.
FAKE_CLAUDE = r'''
import json, sys
args = sys.argv[1:]
def emit(event): print(json.dumps(event), flush=True)
resumed = "--resume" in args
emit({"type": "system", "subtype": "init", "session_id": "s-1"})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}}]}})
if not resumed:
    emit({"type": "result", "subtype": "success", "session_id": "s-1",
          "result": "1. Which database should it use?"})
else:
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": __PLAN_PATH__, "content": "x"}}]}})
    emit({"type": "result", "subtype": "success", "session_id": "s-1",
          "result": "Plan: use Postgres.\nPLAN_READY"})
'''


@pytest.fixture
def fake_claude(monkeypatch):
    home = pathlib.Path(tempfile.mkdtemp())
    plan = home / ".claude" / "plans" / "the-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n\n1. Add the Postgres store.\n", encoding="utf-8")
    script = home / "fake_claude.py"
    script.write_text(FAKE_CLAUDE.replace("__PLAN_PATH__", json.dumps(str(plan))), encoding="utf-8")
    real_exec = asyncio.create_subprocess_exec

    async def spawn(exe, *args, **kwargs):
        return await real_exec(sys.executable, str(script), *args, **kwargs)

    monkeypatch.setattr(claude_plan, "executable", lambda: "claude")
    monkeypatch.setattr(claude_plan.asyncio, "create_subprocess_exec", spawn)
    return plan


@pytest.mark.asyncio
async def test_a_question_then_the_written_plan(fake_claude):
    steps: list[str] = []
    first = await run_turn("Plan auth", pathlib.Path.cwd(), on_step=steps.append)
    assert first.error is None and first.session_id == "s-1"
    assert first.plan is None and "Which database" in first.text, "a question ends the turn"
    assert steps == ["Read src/app.py"]

    second = await run_turn("Postgres", pathlib.Path.cwd(), session_id=first.session_id)
    assert second.plan == "# Plan\n\n1. Add the Postgres store.\n", "the plan file, read back"
    assert "PLAN_READY" not in second.text and second.text == "Plan: use Postgres."


@pytest.mark.asyncio
async def test_no_claude_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(claude_plan, "executable", lambda: None)
    turn = await run_turn("x", pathlib.Path.cwd())
    assert turn.error and "not installed" in turn.error


@pytest.mark.asyncio
async def test_the_command_asks_then_hands_the_approved_plan_to_nova(monkeypatch):
    import novacode_cli.commands.claude_plan as module
    from tests.test_tui_compaction_row import _app

    turns = iter([
        PlanTurn(session_id="s-1", text="Which database?"),
        PlanTurn(session_id="s-1", text="Revised.", plan="# v1"),
        PlanTurn(
            session_id="s-1",
            text="Final.",
            plan="# v2\n\n## Steps\n### 1. Add the migration",
        ),
    ])
    prompts: list[tuple[str, str | None]] = []

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, **_):
        prompts.append((prompt, session_id))
        return next(turns)

    monkeypatch.setattr(module, "run_turn", fake_turn)
    monkeypatch.setattr(module, "executable", lambda: "claude")
    app = _app()
    answers = iter(["Postgres", "Add a migration step"])
    choices = iter(["refine", "manual"])
    ran: list[str] = []

    async def dock(payload):
        return {"response": {"answer": next(answers), "selected_index": None}}

    async def approve(screen):
        return next(choices)

    stored: list[str] = []
    app.session_state.set_approved_plan = stored.append  # the real SessionState API

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

    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "_show_question_dock", dock)
        monkeypatch.setattr(app, "push_screen_wait", approve)
        await app._run_claude_plan("/claude-plan add auth")
        await pilot.pause()

    assert prompts[0][1] is None and "add auth" in prompts[0][0]
    assert prompts[1] == ("Postgres", "s-1"), "the answer resumes the same Claude session"
    assert "Add a migration step" in prompts[2][0] and prompts[2][1] == "s-1", "refine resumes too"
    assert len(ran) == 1 and "# v2\n\n## Steps\n### 1. Add the migration" in ran[0]
    assert len(seeded) == 1 and seeded[0][1]["todos"][0]["content"] == "Add the migration"
    assert seeded[0][0]["configurable"]["thread_id"] == app.session_state.thread_id


@pytest.mark.asyncio
async def test_escape_in_the_dock_ends_planning(monkeypatch):
    import novacode_cli.commands.claude_plan as module
    from tests.test_tui_compaction_row import _app

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, **_):
        return PlanTurn(session_id="s-1", text="Which database?")

    monkeypatch.setattr(module, "run_turn", fake_turn)
    monkeypatch.setattr(module, "executable", lambda: "claude")
    app = _app()
    ran: list[bool] = []

    async def dismissed(payload):
        return {"response": {"answer": "", "selected_index": None}}

    async def run_approved():
        ran.append(True)

    async with app.run_test(size=(120, 40)):
        monkeypatch.setattr(app, "_show_question_dock", dismissed)
        monkeypatch.setattr(app, "_maybe_run_approved_plan", run_approved)
        await app._run_claude_plan("/claude-plan add auth")
    assert ran == []


# -- when Claude Code is not installed ----------------------------------------


def test_the_official_installer_location_counts_before_path_is_refreshed(monkeypatch):
    home = pathlib.Path(tempfile.mkdtemp())
    exe = home / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    monkeypatch.setattr(claude_plan.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_plan.Path, "home", lambda: home)
    assert claude_plan.executable() is None
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert claude_plan.executable() == str(exe)


def test_a_login_failure_says_how_to_log_in():
    assert claude_plan.needs_login("Invalid API key · Please run /login")
    assert not claude_plan.needs_login("rate limited, retry later")


async def _missing(monkeypatch, pick: int, installed: bool = False):
    import novacode_cli.commands.claude_plan as module
    from tests.test_tui_compaction_row import _app

    monkeypatch.setattr(module, "executable", lambda: None)
    app = _app()
    calls: dict[str, list] = {"plan": [], "turn": [], "install": [], "log": []}

    async def choose(screen):
        return pick

    async def nova_plan(text):
        calls["plan"].append(text)

    async def fake_install(on_line):
        on_line("Claude Code installed")
        calls["install"].append(True)
        return installed

    async def fake_turn(*a, **k):
        calls["turn"].append(a)
        return PlanTurn(error="should not run")

    monkeypatch.setattr(module, "install", fake_install)
    monkeypatch.setattr(module, "run_turn", fake_turn)
    async with app.run_test(size=(120, 40)):
        monkeypatch.setattr(app, "push_screen_wait", choose)
        monkeypatch.setattr(app, "_run_plan", nova_plan)
        monkeypatch.setattr(app, "_log", lambda t, *a, **k: calls["log"].append(str(t)))
        await app._run_claude_plan("/claude-plan add auth")
    return calls


@pytest.mark.asyncio
async def test_missing_claude_can_plan_with_nova_instead(monkeypatch):
    calls = await _missing(monkeypatch, pick=1)
    assert calls["plan"] == ["/plan add auth"] and not calls["turn"]


@pytest.mark.asyncio
async def test_missing_claude_installs_then_asks_for_the_one_time_login(monkeypatch):
    calls = await _missing(monkeypatch, pick=0, installed=True)
    assert calls["install"] and not calls["turn"], "a fresh install is not logged in yet"
    assert any("Log in once" in line for line in calls["log"])


@pytest.mark.asyncio
async def test_missing_claude_and_cancel_does_nothing(monkeypatch):
    calls = await _missing(monkeypatch, pick=-1)
    assert not (calls["plan"] or calls["turn"] or calls["install"])


# -- whose account is billed ---------------------------------------------------


@pytest.mark.asyncio
async def test_novas_api_key_does_not_override_the_claude_login(monkeypatch):
    """Nova exports its stored keys; `claude` then billed the API key, not the subscription."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nova")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    envs: list[dict] = []

    async def fake_run(prompt, cwd, *, session_id, on_step, env, **_):
        envs.append(env)
        return PlanTurn(session_id="s", text="ok")

    monkeypatch.setattr(claude_plan, "_run", fake_run)
    turn = await run_turn("x", pathlib.Path.cwd())
    assert turn.error is None and len(envs) == 1
    assert "ANTHROPIC_API_KEY" not in envs[0] and "ANTHROPIC_BASE_URL" not in envs[0]
    assert "PATH" in envs[0] or "Path" in envs[0], "the rest of the environment is kept"


@pytest.mark.asyncio
async def test_without_a_claude_login_novas_key_is_tried_once(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nova")
    envs: list[dict] = []

    async def fake_run(prompt, cwd, *, session_id, on_step, env, **_):
        envs.append(env)
        if "ANTHROPIC_API_KEY" not in env:
            return PlanTurn(error="Not logged in · Please run /login")
        return PlanTurn(session_id="s", text="ok")

    monkeypatch.setattr(claude_plan, "_run", fake_run)
    turn = await run_turn("x", pathlib.Path.cwd())
    assert turn.error is None and len(envs) == 2 and envs[1]["ANTHROPIC_API_KEY"] == "sk-nova"


@pytest.mark.asyncio
async def test_an_empty_credit_balance_is_explained(monkeypatch):
    async def fake_run(prompt, cwd, *, session_id, on_step, env, **_):
        return PlanTurn(error="Credit balance is too low")

    monkeypatch.setattr(claude_plan, "_run", fake_run)
    turn = await run_turn("x", pathlib.Path.cwd())
    assert "no credit left" in turn.error and "Log in once" in turn.error


# -- questions go through Nova's ask_user_question flow -------------------------

QUESTIONS_REPLY = """I need two decisions.

```nova-questions
[{"question": "Which database?", "context": "It decides the store layer.",
  "options": [{"label": "Postgres", "description": "server", "value": "postgres"},
              {"label": "SQLite", "description": "file"}]},
 {"question": "Keep the old API?", "options": ["Yes", "No"]},
 {"question": "Dropped: one option only", "options": ["x"]}]
```"""


def test_questions_are_parsed_out_of_the_reply():
    text, questions = claude_plan.parse_questions(QUESTIONS_REPLY)
    assert text == "I need two decisions."
    assert [q["question"] for q in questions] == ["Which database?", "Keep the old API?"]
    assert questions[0]["context"] == "It decides the store layer."
    assert claude_plan.parse_questions("```nova-questions\nnot json\n```")[1] == []
    assert claude_plan.parse_questions("Which database do you want?") == ("Which database do you want?", [])


async def _ask_through(monkeypatch, *, remote: bool):
    import novacode_cli.commands.claude_plan as module
    from novacode_cli.tools.plan_mode_tools import question_request
    from tests.test_tui_compaction_row import _app

    text, questions = claude_plan.parse_questions(QUESTIONS_REPLY)
    turns = iter([PlanTurn(session_id="s", text=text, questions=questions), PlanTurn(session_id="s", text="ok", plan="# p")])
    prompts: list[str] = []

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, **_):
        prompts.append(prompt)
        return next(turns)

    monkeypatch.setattr(module, "run_turn", fake_turn)
    monkeypatch.setattr(module, "executable", lambda: "claude")
    app = _app()
    app.session_state.set_approved_plan = lambda plan: None
    asked: list[dict] = []
    picks = iter(["Postgres — server", "No"])  # what the dock hands back: the display string

    async def answer(request):
        asked.append(request)
        return {"response": {"answer": next(picks), "selected_index": 0}}

    async def approve(screen):
        return "manual"

    async def run_approved():
        return None

    async with app.run_test(size=(120, 40)):
        monkeypatch.setattr(app, "_ask_remote_question" if remote else "_show_question_dock", answer)
        if remote:
            app._remote_msg = object()
        monkeypatch.setattr(app, "push_screen_wait", approve)
        monkeypatch.setattr(app, "_maybe_run_approved_plan", run_approved)
        await app._run_claude_plan("/claude-plan add a store")
    expected, _ = question_request(questions[0]["question"], questions[0]["options"], questions[0]["context"])
    return asked, expected, prompts


@pytest.mark.asyncio
async def test_planner_questions_are_asked_like_ask_user_question(monkeypatch):
    asked, expected, prompts = await _ask_through(monkeypatch, remote=False)
    assert asked[0] == expected and asked[0]["question_type"] == "structured"
    assert [c["label"] for c in asked[0]["choices"]] == ["Postgres", "SQLite"]
    assert len(asked) == 2, "one ask per question"
    assert "A: postgres" in prompts[1], "the chosen option's value goes back to the planner"
    assert "Q: Keep the old API?\nA: No" in prompts[1]


@pytest.mark.asyncio
async def test_a_remote_session_answers_them_in_the_chat(monkeypatch):
    asked, expected, _ = await _ask_through(monkeypatch, remote=True)
    assert asked[0] == expected, "the remote question flow gets the same request"


@pytest.mark.asyncio
async def test_each_planning_turn_is_one_collapsed_card(monkeypatch):
    import novacode_cli.commands.claude_plan as module
    from textual.widgets import Collapsible

    from novacode_cli.config.config import settings
    from tests.test_tui_compaction_row import _app

    root = settings.get_workspace_root()
    turns = iter([
        PlanTurn(session_id="s", text="Which database?"),
        PlanTurn(session_id="s", text="ok", plan="# p"),
    ])

    async def fake_turn(prompt, cwd, *, session_id=None, on_step=None, **_):
        for name in ("Cargo.toml", "src\lib.rs", "src\main.rs"):
            on_step(f"Read {root}\{name}")
        return next(turns)

    monkeypatch.setattr(module, "run_turn", fake_turn)
    monkeypatch.setattr(module, "executable", lambda: "claude")
    app = _app()
    app.session_state.set_approved_plan = lambda plan: None

    async def dock(payload):
        return {"response": {"answer": "Postgres", "selected_index": None}}

    async def approve(screen):
        return "manual"

    async def run_approved():
        return None

    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "_show_question_dock", dock)
        monkeypatch.setattr(app, "push_screen_wait", approve)
        monkeypatch.setattr(app, "_maybe_run_approved_plan", run_approved)
        await app._run_claude_plan("/claude-plan add a store")
        await pilot.pause()
        cards = [c for c in app._transcript().query(Collapsible) if "Claude Code" in str(c.title)]
        assert [str(c.title) for c in cards] == [
            "◆ Claude Code has questions · 3 steps",
            "◆ Claude Code planned · 3 steps",
        ]
        assert all(c.collapsed for c in cards)
        body = str(cards[0].query_one(".planner-steps").render())
        assert "◦ Read Cargo.toml" in body and "◦ Read src\lib.rs" in body
        assert str(root) not in body, "paths are shown relative to the project"
