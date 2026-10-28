"""Native Code Mode graph contracts, isolated from credentials and external services."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import (
    ToolRuntime,  # noqa: TC002 -- tool decorators resolve runtime annotations.
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command

from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.mcp.code_mode import MCPCodeModeMiddleware
from novacode_cli.security.delegated_approval import HardDenyMiddleware
from novacode_cli.security.policy import get_policy
from tests.test_mcp_code_mode import (
    ScriptedModel,
    execute,
    inventory,
    isolated_policy,  # noqa: F401
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from langchain.agents.middleware.types import ToolCallRequest

    from novacode_cli.mcp.middleware import MCPMiddleware

pytestmark = pytest.mark.timeout(30)


def native_graph(
    code: str,
    tools: Sequence[BaseTool] = (),
    *,
    middleware: Sequence[AgentMiddleware] = (),
    auto_approve: bool = False,
    mcp: MCPMiddleware | None = None,
    **limits: float,
):
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "outer",
                            "name": "MCPCode",
                            "args": {"code": code},
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    return create_agent(
        model,
        tools=list(tools),
        middleware=[
            MCPCodeModeMiddleware(mcp, auto_approve=auto_approve, **limits),
            *([mcp] if mcp else []),
            *middleware,
            HardDenyMiddleware(),
        ],
    )


def data(messages: list[ToolMessage]):
    assert len(messages) == 1
    assert messages[0].status == "success", messages[0].content
    return json.loads(messages[0].content)


async def test_discovery_schema_calls_and_private_intermediates_without_mcp():
    calls = []

    @tool
    def native_read(value: int, runtime: ToolRuntime) -> dict:
        """Read a synthetic value with injected runtime."""
        calls.append(runtime.tool_call_id)
        return {"value": value, "private": "unreturned secret"}

    get_policy().tool_tiers["native_read"] = "allow"
    messages = await execute(
        native_graph(
            """
const catalog = await tools.list();
const schema = await tools.describe('native_read');
const rows = await Promise.all([2,3].map(value=>tools.call('native_read', {value})));
return {names: catalog.tools.map(t=>t.name), schema: schema.schema,
        sum: rows.reduce((n,row)=>n+row.value,0)};
""",
            [native_read],
        )
    )
    result = data(messages)
    assert result["result"]["names"] == ["native_read"]
    assert result["result"]["sum"] == 5
    assert "runtime" not in result["result"]["schema"]["parameters"]["properties"]
    assert "unreturned secret" not in messages[0].content
    assert result["calls"] == 4
    assert len(set(calls)) == 2
    assert "outer" not in calls


@pytest.mark.parametrize("arguments", ['{value:"7"}', "{value:1, runtime:{}}"])
async def test_bad_arguments_rejected_before_execution(arguments: str):
    calls = []

    @tool
    def native_read(value: int) -> int:
        """Read a synthetic value."""
        calls.append(value)
        return value

    get_policy().tool_tiers["native_read"] = "allow"
    messages = await execute(
        native_graph(f"return await tools.call('native_read', {arguments});", [native_read])
    )
    assert messages[0].status == "error"
    assert "arguments" in messages[0].content.lower()
    assert calls == []


@pytest.mark.parametrize(
    ("tier", "auto_approve"), [("ask", False), ("deny", False), ("deny", True)]
)
async def test_native_approval_and_denial_happen_before_execution(tier: str, *, auto_approve: bool):
    calls = []

    @tool
    def native_write() -> str:
        """Record a synthetic write."""
        calls.append(True)
        return "written"

    get_policy().tool_tiers["native_write"] = tier
    messages = await execute(
        native_graph(
            "return await tools.call('native_write');",
            [native_write],
            auto_approve=auto_approve,
        )
    )
    assert messages[0].status == "error"
    assert ("approval" if tier == "ask" else "denied") in messages[0].content.lower()
    assert calls == []


async def test_allowed_writes_and_downstream_guards(tmp_path: Path):
    target = tmp_path / "out.txt"

    @tool
    def native_write(content: str) -> str:
        """Write a temporary test file."""
        target.write_text(content)
        return "written"

    class WorkspaceGuard(AgentMiddleware):
        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
        ) -> ToolMessage | Command:
            if request.tool_call["name"] == "native_write":
                return ToolMessage(
                    content="Workspace denied", status="error", tool_call_id=request.tool_call["id"]
                )
            return await handler(request)

    get_policy().tool_tiers["native_write"] = "allow"
    blocked = await execute(
        native_graph(
            "return await tools.call('native_write', {content:'x'});",
            [native_write],
            middleware=[WorkspaceGuard()],
        )
    )
    assert blocked[0].status == "error"
    assert "Workspace denied" in blocked[0].content
    assert not target.exists()
    allowed = await execute(
        native_graph("return await tools.call('native_write', {content:'ok'});", [native_write])
    )
    assert data(allowed)["result"] == "written"
    assert target.read_text() == "ok"


@pytest.mark.parametrize(
    "name",
    [
        "MCPCode",
        "eval",
        "task",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
        "EnterWorktreeTool",
        "ExitWorktreeTool",
        "write_todos",
        "wait_for_task",
    ],
)
async def test_control_tools_are_not_exposed_or_called(name: str):
    calls = []

    @tool(name)
    def control() -> str:
        """Control tool that must only be called directly."""
        calls.append(True)
        return "unsafe"

    get_policy().tool_tiers[name] = "allow"
    messages = await execute(
        native_graph(
            f"""
const names = (await tools.list()).tools.map(t=>t.name);
let rejected = false;
try {{ await tools.call({json.dumps(name)}); }} catch (e) {{ rejected = true; }}
return {{names, rejected}};
""",
            [] if name == "MCPCode" else [control],
        )
    )
    result = data(messages)
    assert name not in result["result"]["names"]
    assert result["result"]["rejected"]
    assert result["failed_calls"] == 1
    assert calls == []


async def test_declared_command_tools_rejected_before_execution():
    calls = []

    @tool
    def update_graph() -> Command:
        """Return a graph mutation."""
        calls.append(True)
        return Command(update={})

    get_policy().tool_tiers["update_graph"] = "allow"
    messages = await execute(
        native_graph("return await tools.call('update_graph');", [update_graph])
    )
    assert messages[0].status == "error"
    assert "Unknown or unsupported" in messages[0].content
    assert calls == []


async def test_declared_command_on_tool_subclass_rejected_before_execution():
    calls = []

    class GraphTool(BaseTool):
        name: str = "graph_tool"
        description: str = "A graph state mutation implemented as a tool subclass."

        def _run(self) -> Command:
            calls.append(True)
            return Command(update={})

    get_policy().tool_tiers["graph_tool"] = "allow"
    messages = await execute(native_graph("return await tools.call('graph_tool');", [GraphTool()]))
    assert messages[0].status == "error"
    assert "Unknown or unsupported" in messages[0].content
    assert calls == []


async def test_unknown_name_fails_closed():
    messages = await execute(native_graph("return await tools.call('missing');"))
    assert messages[0].status == "error"
    assert "Unknown or unsupported" in messages[0].content


async def test_mcp_deny_does_not_disable_native_and_native_cannot_bypass_mcp(tmp_path: Path):
    @tool("db_read")
    def read(value: int) -> int:
        """Read a synthetic MCP value."""
        return value

    @tool
    def native_read() -> str:
        """Read an ordinary value."""
        return "ok"

    mcp = inventory(tmp_path, read)
    get_policy().tool_tiers["MCPTool"] = "deny"
    get_policy().tool_tiers["native_read"] = "allow"
    messages = await execute(
        native_graph(
            """
const value = await tools.call('native_read');
let rejected = false;
try { await tools.call('db_read', {value:1}); } catch(e) { rejected = true; }
return {value, rejected, names:(await tools.list()).tools.map(t=>t.name)};
""",
            [native_read],
            mcp=mcp,
        )
    )
    result = data(messages)["result"]
    assert result == {"value": "ok", "rejected": True, "names": ["native_read"]}


async def test_catalogs_do_not_leak_between_graphs_or_deferred_tools(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.agents.tool_search import ToolSearchMiddleware

    @tool
    def first_tool() -> str:
        """First graph only."""
        return "first"

    @tool
    def second_tool() -> str:
        """Second graph only."""
        return "second"

    monkeypatch.setattr(ToolSearchMiddleware, "_read_frequent", lambda *_a: asyncio.sleep(0))
    a, b = await asyncio.gather(
        *(
            execute(
                native_graph(
                    "return (await tools.list()).tools.map(t=>t.name);",
                    [target],
                    middleware=[ToolSearchMiddleware()],
                )
            )
            for target in [first_tool, second_tool]
        )
    )
    assert "first_tool" in data(a)["result"]
    assert "second_tool" not in data(a)["result"]
    assert "second_tool" in data(b)["result"]
    assert "first_tool" not in data(b)["result"]


async def test_toggle_off_rejects_native_calls():
    NovaConfig().set_code_mode_enabled(False)
    messages = await execute(native_graph("return await tools.list();"))
    assert messages[0].status == "error"
    assert "disabled" in messages[0].content


async def test_native_call_budget_and_failure_accounting():
    @tool
    def native_read() -> str:
        """Read a value."""
        return "ok"

    get_policy().tool_tiers["native_read"] = "allow"
    messages = await execute(
        native_graph(
            """
await tools.call('native_read');
try { await tools.call('missing'); } catch(e) {}
return await tools.call('native_read');
""",
            [native_read],
            max_calls=2,
        )
    )
    assert messages[0].status == "error"
    result = json.loads(messages[0].content)
    assert result["calls"] == 3
    assert result["failed_calls"] == 2


async def test_real_filesystem_tools_read_write_and_edit(tmp_path: Path):
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    (tmp_path / "input.txt").write_text("hello native code mode", encoding="utf-8")
    fs = FilesystemMiddleware(backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True))
    for name in ["read_file", "write_file", "edit_file"]:
        get_policy().tool_tiers[name] = "allow"
    messages = await execute(
        native_graph(
            """
const text = await tools.call('read_file', {file_path:'/input.txt'});
await tools.call('write_file', {file_path:'/output.txt', content:'hello'});
await tools.call('edit_file', {file_path:'/output.txt', old_string:'hello', new_string:'updated'});
return {text, names:(await tools.list()).tools.map(t=>t.name)};
""",
            middleware=[fs],
        )
    )
    result = data(messages)["result"]
    assert "hello native code mode" in result["text"]
    assert "read_file" in result["names"]
    assert (tmp_path / "output.txt").read_text() == "updated"


async def test_unexpected_command_result_is_visible_error():
    @tool
    def undeclared_update(runtime: ToolRuntime):  # noqa: ANN202 -- deliberately undeclared output.
        """A third-party tool with an undeclared graph mutation."""
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="unapplied",
                        tool_call_id=runtime.tool_call_id,
                    )
                ]
            }
        )

    get_policy().tool_tiers["undeclared_update"] = "allow"
    messages = await execute(
        native_graph("return await tools.call('undeclared_update');", [undeclared_update])
    )
    assert messages[0].status == "error"
    assert "unsupported state mutation" in messages[0].content
    assert "unapplied" not in messages[0].content


async def test_failed_later_approval_does_not_replay_earlier_write():
    calls = []

    @tool
    def allowed_write() -> str:
        """Record an allowed side effect."""
        calls.append(True)
        return "done"

    @tool
    def needs_approval() -> str:
        """A tool that requires approval."""
        calls.append(False)
        return "unsafe"

    get_policy().tool_tiers["allowed_write"] = "allow"
    get_policy().tool_tiers["needs_approval"] = "ask"
    messages = await execute(
        native_graph(
            """
await tools.call('allowed_write');
return await tools.call('needs_approval');
""",
            [allowed_write, needs_approval],
        )
    )
    assert calls == [True]
    assert messages[0].status == "error"
    assert "approval" in messages[0].content


async def test_native_parallel_calls_are_bounded_and_cancelled():
    active = peak = 0
    cancelled = []

    @tool
    async def slow_read() -> str:
        """Wait until the enclosing script times out."""
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        finally:
            active -= 1
        return "unexpected"

    get_policy().tool_tiers["slow_read"] = "allow"
    messages = await execute(
        native_graph(
            """
return await Promise.all([1,2,3,4].map(()=>tools.call('slow_read')));
""",
            [slow_read],
            concurrency=2,
            wall_timeout=0.5,
        )
    )
    assert messages[0].status == "error"
    assert peak == 2
    assert active == 0
    assert cancelled == [True, True]


async def test_native_discovery_pagination_and_argument_bounds():
    @tool
    def first() -> str:
        """First ordinary tool."""
        return "first"

    @tool
    def second() -> str:
        """Second ordinary tool."""
        return "second"

    messages = await execute(
        native_graph(
            """
const first = await tools.list({limit:1});
const second = await tools.list({limit:1, offset:1});
const empty = await tools.list({offset:2});
let bad = 0;
for (const options of [{limit:0}, {offset:-1}, {limit:51}, {limit:'1'}, {other:1}]) {
  try { await tools.list(options); } catch(e) { bad++; }
}
return {first,second,empty,bad};
""",
            [first, second],
        )
    )
    result = data(messages)
    assert result["result"]["first"]["tools"][0]["name"] == "first"
    assert result["result"]["second"]["tools"][0]["name"] == "second"
    assert result["result"]["first"]["total"] == 2
    assert result["result"]["empty"]["tools"] == []
    assert result["result"]["bad"] == 5
    assert result["failed_calls"] == 5


async def test_native_runtime_limits_and_no_mcp_configuration():
    for code, fragment in [
        ("return await mcp.list();", "No MCP tools configured"),
        ("return fetch('https://example.invalid');", "fetch"),
    ]:
        messages = await execute(native_graph(code))
        assert messages[0].status == "error"
        assert fragment in messages[0].content


def test_old_middleware_import_is_the_shared_implementation():
    assert MCPCodeModeMiddleware is CodeModeMiddleware


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(offset=st.integers(min_value=0, max_value=6), limit=st.integers(min_value=1, max_value=50))
async def test_native_catalog_page_matches_registered_inventory(offset: int, limit: int):
    @tool
    def first() -> str:
        """First ordinary tool."""
        return "first"

    @tool
    def second() -> str:
        """Second ordinary tool."""
        return "second"

    result = data(
        await execute(
            native_graph(
                f"return await tools.list({{offset:{offset},limit:{limit}}});",
                [first, second],
            )
        )
    )["result"]
    assert [t["name"] for t in result["tools"]] == ["first", "second"][offset : offset + limit]
    assert result["total"] == 2
    assert result["offset"] == offset


async def test_plan_mode_still_blocks_the_interpreter(tmp_path: Path):
    from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware

    messages = await execute(
        native_graph("return await tools.list();", middleware=[PlanModeMiddleware(tmp_path)])
    )
    assert messages[0].status == "error"
    assert "blocked during planning" in messages[0].content


async def test_real_workspace_broker_blocks_outer_interpreter(tmp_path: Path):
    from novacode_cli.cowork.broker_middleware import CoworkBrokerMiddleware

    messages = await execute(
        native_graph("return await tools.list();", middleware=[CoworkBrokerMiddleware(tmp_path)])
    )
    assert messages[0].status == "error"
    assert "no verified workspace authorization" in messages[0].content


@pytest.mark.parametrize("auto_approve", [False, True])
async def test_core_registers_code_mode_without_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, auto_approve: bool
):
    from unittest.mock import Mock

    from novacode_cli.agents import core_agent
    from novacode_cli.mcp.config import MCPConfig

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(MCPConfig, "load", lambda _self: {})
    monkeypatch.setattr(core_agent.settings, "project_root", root)
    monkeypatch.setattr(core_agent, "_build_skill_sources", lambda: ([], root, root, [], []))
    monkeypatch.setattr(core_agent, "_build_middleware_stack", lambda **_kw: [])
    monkeypatch.setattr(core_agent, "_build_subagent_roster", lambda **_kw: [])
    monkeypatch.setattr("novacode_cli.context.ContextManager.window_size", lambda _self: 32768)
    monkeypatch.setattr("novacode_cli.plugins.loader.discover_enabled_plugins", list)
    monkeypatch.setattr("novacode_cli.agents.invocation.definition_signature", lambda: "test")
    build = Mock(wraps=core_agent.create_deep_agent)
    monkeypatch.setattr(core_agent, "create_deep_agent", build)
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "outer",
                            "name": "MCPCode",
                            "args": {
                                "code": "return (await tools.list({limit:50}))"
                                ".tools.map(t=>t.name);"
                            },
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    graph, _ = core_agent.create_agent_with_config(
        model,
        "test-code-mode",
        [],
        system_prompt="Offline test",
        workspace_root=str(root),
        auto_approve=auto_approve,
    )
    assert "execute" in build.call_args.kwargs["interrupt_on"]
    first_middleware = build.call_args.kwargs["middleware"][0]
    assert isinstance(first_middleware, CodeModeMiddleware)
    messages = await execute(graph, {"configurable": {"thread_id": "native-no-mcp"}})
    assert "read_file" in data(messages)["result"]
    assert "MCPTool" not in data(messages)["result"]
