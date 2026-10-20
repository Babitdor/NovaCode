"""Acceptance tests for approved-plan execution tracking."""

from __future__ import annotations

import asyncio

from langchain.agents.middleware.types import ToolCallRequest

from novacode_cli.agents.plan_execution import (
    PlanTodoGuardMiddleware,
    activate_plan_todo_guard,
    build_plan_todos,
)


def test_plan_steps_and_verification_are_seeded_as_open_todos():
    plan = """# Change

## Steps
### 1. Update the parser
**What:** Keep old syntax working.
### 2. Add regression tests

## Verification
- [ ] `uv run pytest tests/test_parser.py` — all pass
"""

    assert build_plan_todos(plan) == [
        {"content": "Update the parser", "status": "in_progress"},
        {"content": "Add regression tests", "status": "pending"},
        {
            "content": "Verify: `uv run pytest tests/test_parser.py` — all pass",
            "status": "pending",
        },
    ]


def test_unstructured_plan_still_requires_the_entire_plan():
    todos = build_plan_todos("# Plan\n\nUse the existing cache and validate the migration.")
    assert len(todos) == 1
    assert todos[0]["status"] == "in_progress"
    assert "approved plan" in todos[0]["content"].lower()


def test_external_planner_ordered_steps_are_imported():
    plan = """# External plan

## Implementation Plan
1. Inspect the current API
2. Add the compatibility layer

## Validation
- Run the focused tests
"""

    assert build_plan_todos(plan) == [
        {"content": "Inspect the current API", "status": "in_progress"},
        {"content": "Add the compatibility layer", "status": "pending"},
        {"content": "Verify: Run the focused tests", "status": "pending"},
    ]


def test_plan_todo_guard_restores_dropped_steps_and_allows_status_updates():
    steps = ["Update the parser", "Add regression tests"]
    middleware = PlanTodoGuardMiddleware()

    async def call(todos: list[dict[str, str]]) -> list[dict[str, str]]:
        request = ToolCallRequest(
            tool_call={"name": "write_todos", "id": "call-1", "args": {"todos": todos}},
            tool=None,
            state={},
            runtime=None,
        )

        async def handler(request: ToolCallRequest) -> list[dict[str, str]]:
            return request.tool_call["args"]["todos"]

        with activate_plan_todo_guard(steps):
            return await middleware.awrap_tool_call(request, handler)

    dropped = asyncio.run(call([{"content": steps[0], "status": "completed"}]))
    assert dropped == [
        {"content": steps[0], "status": "completed"},
        {"content": steps[1], "status": "pending"},
    ]

    preserved = asyncio.run(
        call(
            [
                {"content": steps[0], "status": "completed"},
                {"content": steps[1], "status": "in_progress"},
                {"content": "Extra implementation detail", "status": "pending"},
            ]
        )
    )
    assert preserved == [
        {"content": steps[0], "status": "completed"},
        {"content": steps[1], "status": "in_progress"},
        {"content": "Extra implementation detail", "status": "pending"},
    ]
