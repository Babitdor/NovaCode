"""Approval entry points must save plans according to who approved them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.commands import plan_handler
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_PLAN = "# Approved change\n\n## Steps\n1. Update the parser\n2. Run the tests\n"


@pytest.fixture(autouse=True)
def plan_folders(isolated_plan_storage: tuple[Path, Path]) -> tuple[Path, Path]:
    return isolated_plan_storage


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("command", ["approval", "off"])
async def test_command_approval_saves_to_the_correct_scope(
    plan_folders: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    *,
    automatic: bool,
) -> None:
    project, global_plans = plan_folders
    state = SimpleNamespace(
        auto_approve=automatic,
        plan_mode_enabled=True,
        plan_content=_PLAN,
        todos=[],
        clear_plan_agent=Mock(),
        set_approved_plan=Mock(),
    )
    monkeypatch.setattr(
        "novacode_cli.ui.question_prompt.prompt_for_plan_approval",
        lambda **_kwargs: {"approved": True, "action": "proceed_auto"},
    )
    if command == "approval":
        assert await plan_handler.handle_plan_approval(None, state, _PLAN)
    else:
        assert await plan_handler._disable_plan_mode(None, state)
    local_files = list((project / ".nova" / "plans").glob("*.md"))
    assert bool(local_files) is not automatic
    global_files = list(global_plans.rglob("*.md"))
    assert len(global_files) == 1
    assert global_files[0].read_text(encoding="utf-8") == _PLAN
    if local_files:
        assert local_files[0].read_text(encoding="utf-8") == _PLAN


@pytest.mark.parametrize("planner_name", ["claude_plan", "codex_plan"])
@pytest.mark.parametrize("choice", ["manual", "auto"])
async def test_external_user_approval_saves_locally_even_with_auto_accept_edits(
    plan_folders: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    planner_name: str,
    choice: str,
) -> None:
    from importlib import import_module

    from tests.test_tui_home_preferences import _app

    planner = import_module(f"novacode_cli.commands.{planner_name}")
    monkeypatch.setattr(planner, "executable", lambda: "offline-planner")
    monkeypatch.setattr(
        planner,
        "run_turn",
        AsyncMock(return_value=planner.PlanTurn(session_id="plan-session", text="", plan=_PLAN)),
    )
    app = _app()
    monkeypatch.setattr(app, "push_screen_wait", AsyncMock(return_value=choice))
    monkeypatch.setattr(app, "_maybe_run_approved_plan", AsyncMock())
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await app._run_external_plan(planner, f"{planner.COMMAND} improve the parser")
    project, global_plans = plan_folders
    local_files = list((project / ".nova" / "plans").glob("*.md"))
    assert len(local_files) == 1
    assert local_files[0].read_text(encoding="utf-8") == _PLAN
    assert len(list(global_plans.rglob("*.md"))) == 1


@pytest.mark.parametrize("automatic", [False, True])
async def test_cached_plan_handoff_is_persisted_even_without_inline_text(
    plan_folders: tuple[Path, Path],
    *,
    automatic: bool,
) -> None:
    from novacode_cli import ui_events
    from novacode_cli.core.agent_loop import iterate_agent_events

    class Agent:
        async def aget_state(self, _config: object) -> SimpleNamespace:
            return SimpleNamespace(values={"messages": []})

        async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
            yield (
                (),
                "updates",
                {
                    "__interrupt__": [
                        SimpleNamespace(
                            id="approval",
                            value={"type": "plan_approval", "plan": ""},
                        )
                    ]
                },
            )

    state = SimpleNamespace(
        thread_id="planning",
        auto_approve=automatic,
        plan_mode_enabled=True,
        plan_execution_handoff=True,
        plan_agent=None,
        todos=None,
        approved_plan_content=_PLAN,
        set_approved_plan=Mock(),
        clear_plan_agent=Mock(),
    )
    async for event in iterate_agent_events("Implement the plan", Agent(), "nova", state):
        if isinstance(event, ui_events.InterruptRequest):
            # The user can approve the plan and then auto-accept its edits.
            state.auto_approve = True
            event.future.set_result({"response": {"approved": True, "mode": "auto"}})
    project, global_plans = plan_folders
    assert bool(list((project / ".nova" / "plans").glob("*.md"))) is not automatic
    assert len(list(global_plans.rglob("*.md"))) == 1
    state.set_approved_plan.assert_called_once_with(_PLAN)


@pytest.mark.parametrize("automatic", [False, True])
def test_resumed_exit_tool_saves_file_based_plan_to_correct_scope(
    plan_folders: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    automatic: bool,
) -> None:
    from novacode_cli.tools import plan_mode_tools

    project, global_plans = plan_folders
    drafts = project / ".nova" / "plans"
    drafts.mkdir(parents=True)
    (drafts / "draft.md").write_text(_PLAN, encoding="utf-8")
    monkeypatch.setattr(
        plan_mode_tools,
        "interrupt",
        lambda _request: {
            "approved": True,
            "auto_approved": automatic,
        },
    )
    assert "Plan saved to" in plan_mode_tools.exit_plan_mode.func()
    assert len(list(drafts.glob("plan-*.md"))) == (0 if automatic else 1)
    assert len(list(global_plans.rglob("plan-*.md"))) == 1


@pytest.mark.parametrize("command", ["approval", "off"])
async def test_rejected_plans_are_not_persisted(
    plan_folders: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    state = SimpleNamespace(
        auto_approve=False,
        plan_mode_enabled=True,
        plan_content=_PLAN,
        todos=[],
        clear_plan_agent=Mock(),
        set_approved_plan=Mock(),
    )
    monkeypatch.setattr(
        "novacode_cli.ui.question_prompt.prompt_for_plan_approval",
        lambda **_kwargs: {"approved": False},
    )
    if command == "approval":
        assert not await plan_handler.handle_plan_approval(None, state, _PLAN)
    else:
        await plan_handler._disable_plan_mode(None, state)
    project, global_plans = plan_folders
    assert not (project / ".nova").exists()
    assert not global_plans.exists()


@pytest.mark.parametrize(
    "template", ["System_Prompt_Nova.jinja", "planning.jinja", "plan_agent.jinja"]
)
def test_agent_instructions_describe_both_plan_locations(template: str) -> None:
    from novacode_cli.prompts import render_template

    prompt = render_template(template)
    assert prompt.count("<plan_storage>") == 1
    assert 'recent_plans(action="list")' in prompt
    assert 'recent_plans(action="read", path=<listed path>)' in prompt
    assert "<workspace>/.nova/plans/" in prompt
    assert "~/.nova/plans/<project-slug>/" in prompt
    assert "automatically approved without asking the user" in prompt
    assert '"Approve and auto-accept edits" is still a user-approved plan' in prompt
