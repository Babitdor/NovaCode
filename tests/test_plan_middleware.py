import pytest
from pathlib import Path
from langchain_core.messages import ToolMessage
from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware


@pytest.mark.anyio
async def test_plan_mode_middleware_blocks_outside_plans_and_ralph(tmp_path: Path):
    workspace = tmp_path
    middleware = PlanModeMiddleware(workspace_root=workspace)

    # 1. Blocked tools should fail immediately
    class FakeToolCall:
        tool_call = {"name": "execute", "id": "t1"}

    async def fake_handler(req):
        return "success"

    res = await middleware.awrap_tool_call(FakeToolCall(), fake_handler)
    assert isinstance(res, ToolMessage)
    assert "[Plan Mode]" in res.content
    assert res.status == "error"

    # 2. write_file outside allowed dirs should be blocked
    class FakeWriteOutside:
        tool_call = {
            "name": "write_file",
            "id": "t2",
            "args": {"path": str(workspace / "src" / "main.py")}
        }

    res = await middleware.awrap_tool_call(FakeWriteOutside(), fake_handler)
    assert isinstance(res, ToolMessage)
    assert "writes are only allowed" in res.content
    assert res.status == "error"

    # 3. write_file inside plans should be allowed
    class FakeWritePlans:
        tool_call = {
            "name": "write_file",
            "id": "t3",
            "args": {"path": str(workspace / ".nova" / "plans" / "plan.md")}
        }

    res = await middleware.awrap_tool_call(FakeWritePlans(), fake_handler)
    assert res == "success"

    # 4. write_file inside ralph should be allowed
    class FakeWriteRalph:
        tool_call = {
            "name": "write_file",
            "id": "t4",
            "args": {"path": str(workspace / ".nova" / "ralph" / "progress.md")}
        }

    res = await middleware.awrap_tool_call(FakeWriteRalph(), fake_handler)
    assert res == "success"

    # 5. Virtual paths starting with / inside plans should be allowed
    class FakeVirtualPlans:
        tool_call = {
            "name": "write_file",
            "id": "t5",
            "args": {"path": "/.nova/plans/plan.md"}
        }

    res = await middleware.awrap_tool_call(FakeVirtualPlans(), fake_handler)
    assert res == "success"

    # 6. Virtual paths starting with / inside ralph should be allowed
    class FakeVirtualRalph:
        tool_call = {
            "name": "write_file",
            "id": "t6",
            "args": {"path": "/.nova/ralph/progress.md"}
        }

    res = await middleware.awrap_tool_call(FakeVirtualRalph(), fake_handler)
    assert res == "success"


def test_hitl_approval_allows_ralph_and_plans_in_plan_mode():
    from novacode_cli.ui.hitl_approval import check_plan_mode_blocked

    # 1. Blocked when writing outside
    outside_request = {
        "action_requests": [
            {
                "name": "write_file",
                "args": {"file_path": "src/main.py"}
            }
        ]
    }
    blocked, resp = check_plan_mode_blocked(outside_request, plan_mode_enabled=True)
    assert blocked is True
    assert "writes/edits are only allowed" in resp["decisions"][0]["message"]

    # 2. Allowed when writing to .nova/plans/
    plans_request = {
        "action_requests": [
            {
                "name": "write_file",
                "args": {"file_path": ".nova/plans/plan.md"}
            }
        ]
    }
    blocked, resp = check_plan_mode_blocked(plans_request, plan_mode_enabled=True)
    assert blocked is False

    # 3. Allowed when writing to .nova/ralph/
    ralph_request = {
        "action_requests": [
            {
                "name": "write_file",
                "args": {"file_path": ".nova/ralph/progress.md"}
            }
        ]
    }
    blocked, resp = check_plan_mode_blocked(ralph_request, plan_mode_enabled=True)
    assert blocked is False
