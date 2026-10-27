"""Plan mode blocks MCP mutation before tools run, regardless of approval defaults."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from novacode_cli import ui_events as ev
from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware
from novacode_cli.config.plan_mode import active_plan_session, is_plan_path, plan_tool_rejection
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.mcp.generic import build_mcp_tool
from novacode_cli.tools.plan_mode_tools import enter_plan_mode
from novacode_cli.ui.hitl_approval import check_plan_mode_blocked, evaluate_tool_actions
from tests.test_mcp_code_mode import (
    ScriptedModel,
    execute,
    graph,
    inventory,
    isolated_policy,  # noqa: F401
)
from tests.test_prime import Model, call, state

if TYPE_CHECKING:
    from pathlib import Path

MUTATIONS = [
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "rename_symbol",
    "replace_content",
    "create_text_file",
    "delete_lines",
    "replace_lines",
    "insert_at_line",
    "execute_shell_command",
    "write_memory",
    "edit_memory",
    "delete_memory",
    "onboarding",
    "activate_project",
    "unknown_future_tool",
]


@pytest.mark.parametrize("raw", MUTATIONS)
@pytest.mark.parametrize("prefix", ["serena_", "mcp__serena__"])
async def test_serena_mutations_never_reach_handler(raw: str, prefix: str):
    request = ToolCallRequest(
        tool_call={"name": prefix + raw, "id": "write", "args": {}},
        tool=None,
        state={},
        runtime=None,
    )
    handler = AsyncMock()
    result = await PlanModeMiddleware().awrap_tool_call(request, handler)
    assert result.status == "error"
    handler.assert_not_called()
    sync_handler = Mock()
    assert PlanModeMiddleware().wrap_tool_call(request, sync_handler).status == "error"
    sync_handler.assert_not_called()
    action = {"action_requests": [request.tool_call]}
    assert check_plan_mode_blocked(action, plan_mode_enabled=True)[0]
    assert (
        evaluate_tool_actions(
            action,
            SimpleNamespace(auto_approve=True),
            plan_mode_enabled=True,
        )[0]["type"]
        == "reject"
    )


@pytest.mark.parametrize("raw", ["find_symbol", "read_file", "search_for_pattern", "read_memory"])
def test_serena_reads_remain_available(raw: str):
    assert plan_tool_rejection("serena_" + raw, {}) is None
    assert (
        plan_tool_rejection(
            "MCPTool",
            {
                "action": "call",
                "server_name": "serena",
                "tool_name": raw,
            },
        )
        is None
    )


def test_mcp_metadata_is_conservative_and_never_overrides_serena_restrictions():
    assert plan_tool_rejection("custom_lookup", {}, metadata={"nova_mcp_server": "db"})
    assert (
        plan_tool_rejection(
            "custom_lookup",
            {},
            metadata={
                "nova_mcp_server": "db",
                "readOnlyHint": True,
            },
        )
        is None
    )
    assert plan_tool_rejection(
        "custom_lookup",
        {},
        metadata={
            "nova_mcp_server": "db",
            "readOnlyHint": True,
            "destructiveHint": True,
        },
    )
    assert plan_tool_rejection(
        "renamed_tool",
        {},
        metadata={
            "nova_mcp_server": "serena",
            "nova_mcp_tool": "replace_symbol_body",
            "readOnlyHint": True,
        },
    )
    assert plan_tool_rejection(
        "db_shell",
        {},
        metadata={
            "nova_mcp_server": "db",
            "nova_mcp_tool": "shell",
            "readOnlyHint": True,
        },
    )
    assert plan_tool_rejection(
        "mcp__serena__find_symbol",
        {},
        metadata={
            "nova_mcp_server": "serena",
            "nova_mcp_tool": "replace_symbol_body",
            "readOnlyHint": True,
        },
    )


def test_plan_paths_reject_traversal_and_lookalikes(tmp_path: Path):
    assert is_plan_path("/.nova/plans/plan.md", tmp_path)
    assert is_plan_path(".nova/ralph/progress.md", tmp_path)
    for path in (
        "/.nova/plans/../../src.py",
        ".nova/plans/../src.py",
        ".nova/plans-evil/file.py",
        "outside/.nova/plans/file.py",
    ):
        assert not is_plan_path(path, tmp_path)


def test_plan_paths_reject_symlink_escape(tmp_path: Path):
    plans = tmp_path / ".nova" / "plans"
    plans.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (plans / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable for this test process")
    assert not is_plan_path("/.nova/plans/linked/file.py", tmp_path)


async def test_live_plan_session_controls_main_agent_and_restores_after_exit():
    session = SimpleNamespace(plan_mode_enabled=False, auto_approve=True)
    token = active_plan_session.set(session)
    request = ToolCallRequest(
        tool_call={"name": "serena_replace_symbol_body", "id": "edit", "args": {}},
        tool=None,
        state={},
        runtime=None,
    )
    handler = AsyncMock(return_value="executed")
    middleware = PlanModeMiddleware(always_on=False)
    try:
        assert await middleware.awrap_tool_call(request, handler) == "executed"
        enter_plan_mode.invoke({"reason": "Investigate"})
        assert session.plan_mode_enabled
        assert (await middleware.awrap_tool_call(request, handler)).status == "error"
        assert handler.await_count == 1
        session.plan_mode_enabled = False
        assert await middleware.awrap_tool_call(request, handler) == "executed"
    finally:
        active_plan_session.reset(token)
    assert active_plan_session.get() is None


@pytest.mark.timeout(30)
async def test_shared_loop_binds_plan_guard_and_isolates_concurrent_sessions():
    calls = []

    @tool("serena_replace_symbol_body")
    async def mutation() -> str:
        """Record execution of a synthetic edit."""
        await asyncio.sleep(0)
        calls.append("edit")
        return "edited"

    async def run(name: str, *, planning: bool) -> None:
        agent = create_agent(
            Model(responses=[call(mutation.name, name), AIMessage(content="done")]),
            tools=[mutation],
            middleware=[PlanModeMiddleware(always_on=False)],
        )
        session = state(name, enabled=False)
        session.auto_approve = True
        session.plan_mode_enabled = planning
        events = [e async for e in iterate_agent_events("Investigate", agent, "nova", session)]
        assert not [e for e in events if isinstance(e, ev.Error)]
        assert active_plan_session.get() is None

    await asyncio.gather(run("planning", planning=True), run("ordinary", planning=False))
    assert calls == ["edit"]


@pytest.mark.timeout(30)
async def test_real_generic_mcp_routing_and_code_mode_cannot_mutate(tmp_path: Path):
    calls = []

    @tool("serena_replace_symbol_body")
    def replace_symbol_body() -> str:
        """Synthetic mutation that must never execute in planning."""
        calls.append("write")
        return "modified"

    mcp = inventory(tmp_path, replace_symbol_body)
    # Inventory helper uses db; use authoritative server/raw identity here.
    replace_symbol_body.metadata = {
        "nova_mcp_server": "serena",
        "nova_mcp_tool": "replace_symbol_body",
    }
    mcp._tools_cache[0].update(server="serena", skill_alias="replace_symbol_body")
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "generic",
                            "name": "MCPTool",
                            "args": {
                                "action": "call",
                                "server_name": "serena",
                                "tool_name": "replace_symbol_body",
                                "arguments": {},
                            },
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    guarded = create_agent(
        model,
        tools=[build_mcp_tool(mcp)],
        middleware=[mcp, PlanModeMiddleware()],
    )
    result = await guarded.ainvoke({"messages": [HumanMessage(content="Plan only")]})
    responses = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert responses[0].status == "error"
    assert "Plan Mode" in str(responses[0].content)
    assert calls == []
    responses = await execute(
        graph(
            mcp,
            'return await mcp.call("serena", "replace_symbol_body", {});',
            middleware=[PlanModeMiddleware()],
            auto_approve=True,
        )
    )
    assert responses[0].status == "error"
    assert "Plan Mode" in str(responses[0].content)
    assert calls == []
