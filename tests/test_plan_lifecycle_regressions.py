"""Approval continuation and todo state must survive the planning handoff."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from textual.widgets import Static

from novacode_cli.agents.plan_execution import (
    PlanTodoGuardMiddleware,
    activate_plan_todo_guard,
    build_plan_todos,
)
from novacode_cli.tui.app import NovaApp

PLAN = """# Landing page
## Steps
1. Update the landing page
2. Serve the preview
### Verification
- Serve the preview
- Verify: Check the responsive layout
"""


def test_nested_verification_is_imported_once():
    todos = build_plan_todos(PLAN)
    assert [item["content"] for item in todos] == [
        "Update the landing page",
        "Serve the preview",
        "Verify: Check the responsive layout",
    ]


def test_numbered_verification_is_not_imported_as_implementation():
    todos = build_plan_todos("# Plan\n1. Change the page\n## Verification\n1. Check it")
    assert [item["content"] for item in todos] == ["Change the page", "Verify: Check it"]


def test_similar_case_sensitive_commands_remain_distinct():
    todos = build_plan_todos("## Steps\n1. Read `Makefile`\n2. Read `makefile`")
    assert len(todos) == 2


def _request(todos: list, state: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "write_todos", "id": "update", "args": {"todos": todos}},
        tool=None,
        state=state or {},
        runtime=None,
    )


@pytest.mark.asyncio
async def test_partial_update_keeps_prior_completion_and_missing_checks():
    required = ["Edit the page", "Verify: Check the page"]
    previous = [{"content": required[0], "status": "completed"}]
    request = _request(
        [
            {"content": "Check the page", "status": "in_progress"},
            {"content": "Resize the browser", "status": "pending"},
        ],
        {"todos": previous},
    )
    handler = AsyncMock(return_value="success")
    with activate_plan_todo_guard(required):
        result = await PlanTodoGuardMiddleware().awrap_tool_call(request, handler)
    assert result == "success"
    updated = handler.call_args.args[0].tool_call["args"]["todos"]
    assert updated == [
        {"content": required[0], "status": "completed"},
        {"content": required[1], "status": "in_progress"},
        {"content": "Resize the browser", "status": "pending"},
    ]
    assert len(request.tool_call["args"]["todos"]) == 2
    assert request.state["todos"] == previous


@pytest.mark.asyncio
async def test_planning_checklists_do_not_inherit_execution_requirements():
    request = _request([{"content": "Research alternatives", "status": "in_progress"}])
    handler = AsyncMock(return_value="success")
    with activate_plan_todo_guard(["Old execution step"], is_planning=lambda: True):
        await PlanTodoGuardMiddleware().awrap_tool_call(request, handler)
    handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_planning_policy_allows_tracking_but_still_blocks_implementation():
    from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware
    from novacode_cli.ui.hitl_approval import check_plan_mode_blocked

    handler = AsyncMock(return_value="tracked")
    request = _request([{"content": "Read the files", "status": "pending"}])
    assert await PlanModeMiddleware().awrap_tool_call(request, handler) == "tracked"
    blocked, _ = check_plan_mode_blocked(
        {
            "action_requests": [{"name": "write_todos", "args": request.tool_call["args"]}],
        },
        plan_mode_enabled=True,
    )
    assert not blocked
    blocked, _ = check_plan_mode_blocked(
        {
            "action_requests": [{"name": "shell", "args": {"command": "make"}}],
        },
        plan_mode_enabled=True,
    )
    assert blocked


@pytest.mark.asyncio
async def test_concurrent_plan_contracts_are_isolated():
    async def update(name: str) -> list:
        request = _request([])
        handler = AsyncMock(side_effect=lambda r: r.tool_call["args"]["todos"])
        with activate_plan_todo_guard([name]):
            await asyncio.sleep(0)
            return await PlanTodoGuardMiddleware().awrap_tool_call(request, handler)

    first, second = await asyncio.gather(update("First plan"), update("Second plan"))
    assert first == [{"content": "First plan", "status": "pending"}]
    assert second == [{"content": "Second plan", "status": "pending"}]


def _enable_handoff(app: NovaApp) -> None:
    session = app.session_state
    session.approved_plan_content = None

    def consume() -> str | None:
        approved = session.approved_plan_content
        session.approved_plan_content = None
        return approved

    def clear() -> None:
        session.plan_mode_enabled = False
        session.plan_agent = None

    session.consume_approved_plan = consume
    session.clear_plan_agent = clear
    session.set_approved_plan = lambda content: setattr(session, "approved_plan_content", content)


def test_pending_approval_handoff_defers_exclusive_async_reports():
    from types import SimpleNamespace

    app = SimpleNamespace(
        _pending_async_completion_ids=["finished-task"],
        _turn_active=False,
        session_state=SimpleNamespace(approved_plan_content=PLAN),
        _async_report_scheduled=False,
        set_timer=Mock(),
        _dispatch_pending_async_report=Mock(),
    )
    NovaApp._schedule_pending_async_report(app)
    app.set_timer.assert_not_called()
    NovaApp._dispatch_pending_async_report(app)
    assert app._pending_async_completion_ids == ["finished-task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("design", ["classic-ui", "dashboard-ui"])
async def test_approval_runs_execution_and_collapse_keeps_the_checklist(
    monkeypatch: pytest.MonkeyPatch,
    design: str,
):
    from tests.test_agents_screen import _app

    app = _app()
    _enable_handoff(app)
    prompts = []

    async def stream(text: str, _assistant_id: str | None) -> None:
        prompts.append(text)
        if len(prompts) == 1:
            app.session_state.approved_plan_content = PLAN

    monkeypatch.setattr(app, "_do_stream", stream)
    async with app.run_test(size=(100, 35)) as pilot:
        app.screen.remove_class("classic-ui", "dashboard-ui")
        app.screen.add_class(design)
        await app._stream_prompt("Plan the landing page")
        await pilot.pause()
        assert len(prompts) == 2
        assert PLAN in prompts[1]
        assert app._todos == build_plan_todos(PLAN)
        dock = app.query_one("#todo-dock", Static)
        assert dock.has_class("active")
        app.action_toggle_todos()
        await pilot.pause()
        assert dock.has_class("active")
        assert dock.has_class("collapsed")
        assert dock.size.height == 1
        assert "0/3" in str(dock.render())
        app.action_toggle_todos()
        await pilot.pause()
        assert dock.has_class("active")
        assert dock.size.height > 1


@pytest.mark.asyncio
async def test_cancelled_planning_does_not_start_execution(monkeypatch: pytest.MonkeyPatch):
    from tests.test_agents_screen import _app

    app = _app()
    _enable_handoff(app)

    async def cancel(_text: str, _assistant_id: str | None) -> None:
        app.session_state.approved_plan_content = PLAN
        raise asyncio.CancelledError

    handoff = AsyncMock()
    monkeypatch.setattr(app, "_do_stream", cancel)
    monkeypatch.setattr(app, "_maybe_run_approved_plan", handoff)
    async with app.run_test():
        await app._stream_prompt("Plan the landing page")
        handoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_manual_approval_continues_through_the_real_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    from types import SimpleNamespace

    from novacode_cli import agent_stream, ui_events
    from novacode_cli.core import agent_loop
    from tests.test_agents_screen import _app
    from tests.test_tui_app import _Chunk, _FakeAgent

    class Agent(_FakeAgent):
        def __init__(self) -> None:
            self.calls = []

        async def astream(self, inp: dict, **_kwargs: object) -> AsyncIterator[tuple]:
            self.calls.append(inp)
            if len(self.calls) == 1:
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": [
                            SimpleNamespace(
                                id="plan-review",
                                value={"type": "plan_approval", "plan": PLAN},
                            )
                        ]
                    },
                )
            else:
                yield (
                    (),
                    "messages",
                    (
                        _Chunk(
                            "execution",
                            [
                                {
                                    "type": "text",
                                    "text": "Starting implementation",
                                }
                            ],
                        ),
                        {},
                    ),
                )

    app = _app()
    _enable_handoff(app)
    app.agent = Agent()
    app.session_state.auto_approve = False
    app.session_state.plan_mode_enabled = True
    monkeypatch.setattr(agent_stream, "_maybe_verifier", lambda: None)
    monkeypatch.setattr(agent_loop, "_save_unresumed_plan", lambda *_args: None)

    async def approve(event: ui_events.InterruptRequest) -> None:
        assert event.kind == "plan"
        event.future.set_result(
            {
                "response": {"approved": True, "mode": "manual"},
                "state_update": {"plan_mode_enabled": False},
            }
        )

    monkeypatch.setattr(app, "_handle_interrupt", approve)
    async with app.run_test():
        await app._stream_prompt("Plan the landing page")
        assert len(app.agent.calls) == 2
        assert PLAN in app.agent.calls[1]["messages"][0]["content"]
        assert app.session_state.approved_plan_content is None
        assert not app.session_state.plan_mode_enabled
