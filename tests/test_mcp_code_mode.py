"""Executable MCP Code Mode contract; no credentials or external services."""

from __future__ import annotations

import asyncio
import json
import sys
from contextvars import ContextVar
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from novacode_cli.mcp.code_mode import MCPCodeModeMiddleware
from novacode_cli.mcp.middleware import MCPMiddleware
from novacode_cli.security.delegated_approval import HardDenyMiddleware
from novacode_cli.security.policy import get_policy
from novacode_cli.security.prompt_injection import PromptInjectionMiddleware

pytestmark = pytest.mark.timeout(30)


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch, tmp_path):
    import novacode_cli.config.config as config_module
    from novacode_cli.security import policy

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr(policy, "_read_json", lambda path: {})
    monkeypatch.setattr(policy, "_CACHED_POLICY", policy.load_policy(tmp_path))
    from novacode_cli.security import session_allow

    monkeypatch.setattr(session_allow, "_SESSION_ALLOW", None)
    monkeypatch.setattr(session_allow, "_SESSION_ALLOWS", {})


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, *args, **kwargs):
        return self


def inventory(tmp_path, target):
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers": {}}')
    mcp = MCPMiddleware(config)
    mcp._tools_discovered = True
    target.metadata = {"nova_mcp_server": "db"}
    mcp.tools.append(target)
    mcp._tools_cache.append(
        {
            "server": "db",
            "skill_alias": "read",
            "name": target.name,
            "description": target.description,
        }
    )
    get_policy().tool_tiers[target.name] = "allow"
    return mcp


def graph(mcp, code, *, middleware=(), auto_approve=False, **limits):
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "outer", "name": "MCPCode", "args": {"code": code}}],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    return create_agent(
        model,
        middleware=[
            MCPCodeModeMiddleware(mcp, auto_approve=auto_approve, **limits),
            mcp,
            *middleware,
            HardDenyMiddleware(),
            PromptInjectionMiddleware(),
        ],
    )


async def execute(g, config=None):
    output = await g.ainvoke(
        {"messages": [HumanMessage(content="Run synthetic MCP workflow")]},
        {**(config or {}), "recursion_limit": 20},
    )
    return [m for m in output["messages"] if isinstance(m, ToolMessage)]


async def test_discovery_calls_filtering_and_private_intermediates(tmp_path):
    calls = []

    @tool("db_read")
    def read(value: int) -> dict:
        """Read a typed synthetic value."""
        calls.append(value)
        return {"value": value, "private_intermediate": "omitted_" * 5000}

    mcp = inventory(tmp_path, read)
    messages = await execute(
        graph(
            mcp,
            """
const catalog = await mcp.list();
const schema = await mcp.describe("db", "read");
const rows = await Promise.all([2,3].map(value => mcp.call("db", "read", {value})));
return {names: catalog.tools.map(t=>t.tool_name), required: schema.schema.parameters.required,
        sum: rows.reduce((total,row)=>total+row.value,0)};
""",
        )
    )
    assert sorted(calls) == [2, 3]
    assert len(messages) == 1
    data = json.loads(messages[0].content)
    assert data["result"] == {"names": ["read"], "required": ["value"], "sum": 5}
    assert "omitted_" not in messages[0].content
    assert messages[0].status == "success"


@pytest.mark.parametrize("denied", ["db_read", "MCPTool", "MCPCode"])
async def test_denies_survive_auto_approve(tmp_path, denied):
    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    mcp = inventory(tmp_path, read)
    get_policy().tool_tiers[denied] = "deny"
    messages = await execute(
        graph(mcp, 'return await mcp.call("db","read",{value:7});', auto_approve=True)
    )
    assert not calls
    assert messages[0].status == "error"
    assert "denied" in messages[0].content.lower()


async def test_ask_requires_normal_approval_path(tmp_path):
    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    mcp = inventory(tmp_path, read)
    get_policy().tool_tiers["db_read"] = "ask"
    code = 'return await mcp.call("db","read",{value:7});'
    messages = await execute(graph(mcp, code))
    assert not calls
    assert messages[0].status == "error"
    assert "approval" in messages[0].content.lower()
    messages = await execute(graph(mcp, code, auto_approve=True))
    assert calls == [7]
    assert json.loads(messages[0].content)["result"] == 7


@pytest.mark.parametrize(
    "code, error",
    [
        ('return await mcp.call("db","read",{value:"7"});', "arguments"),
        ('return await mcp.call("db","missing",{});', "disabled"),
        ('return await mcp.call("other","read",{value:7});', "disabled"),
    ],
)
async def test_schema_and_inventory_validation_without_provider_strict_mode(
    tmp_path, monkeypatch, code, error
):
    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    monkeypatch.setenv("NOVA_STRICT_TOOLS", "off")
    messages = await execute(graph(inventory(tmp_path, read), code))
    assert not calls
    assert messages[0].status == "error"
    assert error in messages[0].content.lower()


async def test_nested_middleware_context_and_trace_ids(tmp_path):
    observed = []
    marker = ContextVar("mcp_test_marker", default="missing")

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        return value

    class Observe(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            observed.append((request.tool_call["name"], request.tool_call["id"], marker.get()))
            return await handler(request)

    class Traces(BaseCallbackHandler):
        def __init__(self):
            self.names = []
            self.outputs = []

        def on_tool_start(self, serialized, input_str, **kwargs):
            self.names.append(serialized["name"])

        def on_tool_end(self, output, **kwargs):
            if isinstance(output, ToolMessage) and output.name == "MCPCode":
                self.outputs.append(output.content)

    trace = Traces()
    token = marker.set("session-context")
    try:
        await execute(
            graph(
                inventory(tmp_path, read),
                """
return await Promise.all([1,2].map(value=>mcp.call("db","read",{value})));
""",
                middleware=[Observe()],
            ),
            {"callbacks": [trace]},
        )
    finally:
        marker.reset(token)
    nested = [row for row in observed if row[0] == "db_read"]
    assert len(nested) == 2
    assert len({row[1] for row in nested}) == 2
    assert all(row[2] == "session-context" for row in nested)
    assert trace.names.count("db_read") == 2
    assert len(trace.outputs) == 1
    assert json.loads(trace.outputs[0])["result"] == [1, 2]


async def test_downstream_denial_stops_execution(tmp_path):
    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    class Workspace(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "db_read":
                return ToolMessage(
                    content="workspace denied", status="error", tool_call_id=request.tool_call["id"]
                )
            return await handler(request)

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            'return await mcp.call("db","read",{value:7});',
            middleware=[Workspace()],
        )
    )
    assert not calls
    assert messages[0].status == "error"
    assert "workspace denied" in messages[0].content


async def test_parallel_calls_are_actually_concurrent_and_bounded(tmp_path):
    active = peak = 0

    @tool("db_read")
    async def read(value: int) -> int:
        """Read a delayed number."""
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return value
        finally:
            active -= 1

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            'return await Promise.all([1,2,3,4].map(value=>mcp.call("db","read",{value})));',
            concurrency=2,
        )
    )
    assert json.loads(messages[0].content)["result"] == [1, 2, 3, 4]
    assert peak == 2
    assert active == 0


async def test_budget_errors_and_caught_failures_are_reported(tmp_path):
    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            """
for(let i=0; i<5; i++) {
  try { await mcp.call("db","read",{value:i}); } catch(e) {}
}
return "finished";
""",
            max_calls=2,
        )
    )
    data = json.loads(messages[0].content)
    assert calls == [0, 1]
    assert data["failed_calls"] == 3
    assert data["calls"] == 5


@pytest.mark.parametrize(
    "code, fragment",
    [
        ("while(true) {}", "timeout"),
        ("throw new Error('synthetic failure');", "synthetic failure"),
        ("return fetch('https://example.invalid');", "fetch"),
        ("return process.env;", "process"),
        ("return require('fs');", "require"),
    ],
)
async def test_runtime_bounds_and_missing_host_apis(tmp_path, code, fragment):
    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        return value

    messages = await execute(graph(inventory(tmp_path, read), code, cpu_timeout=0.05))
    assert messages[0].status == "error"
    assert fragment in messages[0].content.lower()


async def test_large_results_are_explicitly_bounded_and_fresh_calls_are_isolated(tmp_path):
    @tool("db_read")
    def read(value: int) -> str:
        """Read a number."""
        return str(value)

    mcp = inventory(tmp_path, read)
    messages = await execute(
        graph(mcp, 'globalThis.secret = "previous"; return "x".repeat(15000);')
    )
    data = json.loads(messages[0].content)
    assert data["truncated"] is True
    assert len(data["result"]) == 12000
    messages = await execute(graph(mcp, "return typeof secret;"))
    assert json.loads(messages[0].content)["result"] == "undefined"


async def test_nested_timeout_cancels_pending_tool(tmp_path):
    cancelled = asyncio.Event()

    @tool("db_read")
    async def read(value: int) -> int:
        """Read a slow number."""
        try:
            await asyncio.sleep(20)
            return value
        finally:
            cancelled.set()

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            'return await mcp.call("db","read",{value:1});',
            call_timeout=0.05,
        )
    )
    assert messages[0].status == "error"
    assert "timeout" in messages[0].content.lower()
    assert cancelled.is_set()


async def test_outer_cancellation_stops_pending_calls_and_worker(tmp_path):
    import threading

    started, stopped = asyncio.Event(), asyncio.Event()

    @tool("db_read")
    async def read(value: int) -> int:
        """Read a slow number."""
        started.set()
        try:
            await asyncio.sleep(20)
            return value
        finally:
            stopped.set()

    baseline = sum(t.name == "nova-mcp-code" for t in threading.enumerate())
    task = asyncio.create_task(
        execute(graph(inventory(tmp_path, read), 'return await mcp.call("db","read",{value:1});'))
    )
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert sum(t.name == "nova-mcp-code" for t in threading.enumerate()) == baseline


async def test_wall_deadline_and_oversized_intermediate(tmp_path):
    active = False

    @tool("db_read")
    async def read(value: int) -> str:
        """Read a delayed or oversized payload."""
        nonlocal active
        if value == 2:
            return "x" * 1_000_001
        active = True
        try:
            await asyncio.sleep(20)
            return str(value)
        finally:
            active = False

    mcp = inventory(tmp_path, read)
    messages = await execute(
        graph(mcp, 'return await mcp.call("db","read",{value:1});', wall_timeout=0.05)
    )
    assert messages[0].status == "error"
    assert "timeout" in messages[0].content.lower()
    # A deadline can expire during startup, before any tool has begun.
    assert active is False
    messages = await execute(graph(mcp, 'return await mcp.call("db","read",{value:2});'))
    assert messages[0].status == "error"
    assert "exceeds" in messages[0].content.lower()


async def test_runtime_credential_cannot_be_forwarded_through_nested_call(tmp_path, monkeypatch):
    secret = "sk-synthetic-code-mode-credential-123456789"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    calls = []

    @tool("db_read")
    def read(value: str) -> str:
        """Return synthetic data, never make a request."""
        calls.append(value)
        return value

    # Build the value in JavaScript: the outer code itself has no literal key.
    code = (
        'return await mcp.call("db","read",{value:'
        + json.dumps(secret[:20])
        + "+"
        + json.dumps(secret[20:])
        + "});"
    )
    messages = await execute(graph(inventory(tmp_path, read), code))
    assert not calls
    assert messages[0].status == "error"
    assert "credential" in messages[0].content.lower()
    assert secret not in messages[0].content


@pytest.mark.timeout(30)
async def test_real_stdio_server_schema_filtering_and_persistent_state(tmp_path):
    from novacode_cli.mcp.middleware import _fetch_raw_tools

    config = tmp_path / "mcp.json"
    fixture = Path(__file__).parent / "fixtures" / "generic_mcp_stdio.py"
    connection = {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]}
    config.write_text(json.dumps({"mcpServers": {"peer": {**connection, "tools": ["increment"]}}}))
    mcp = MCPMiddleware(config)
    raw = await _fetch_raw_tools(connection)
    mcp.tools = mcp._convert_raw_tools("peer", connection, raw)
    mcp._tools_discovered = True
    get_policy().tool_tiers["peer_increment"] = "allow"
    try:
        messages = await execute(
            graph(
                mcp,
                """
const catalog = await mcp.list();
const first = await mcp.call("peer","increment",{amount:2});
const second = await mcp.call("peer","increment",{amount:3});
return {names:catalog.tools.map(t=>t.tool_name), count:second.count, before:first.count};
""",
            )
        )
        assert messages[0].status == "success", messages[0].content
        data = json.loads(messages[0].content)
        assert data["result"] == {"names": ["increment"], "count": 5, "before": 2}
        assert "payload " not in messages[0].content
    finally:
        await mcp._session_pool.aclose()


async def test_plan_mode_prevents_script_execution(tmp_path):
    from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware

    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            'return await mcp.call("db","read",{value:1});',
            middleware=[PlanModeMiddleware()],
        )
    )
    assert not calls
    assert messages[0].status == "error"
    assert "Plan Mode" in messages[0].content


async def test_code_policy_ask_is_not_implicitly_approved(tmp_path):
    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        return value

    mcp = inventory(tmp_path, read)
    get_policy().tool_tiers["MCPCode"] = "ask"
    messages = await execute(graph(mcp, "return 1;"))
    assert messages[0].status == "error"
    assert "approval" in messages[0].content


async def test_inventory_refresh_and_malformed_raw_bridge_calls(tmp_path):
    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    mcp = inventory(tmp_path, read)
    mcp.tools.clear()
    messages = await execute(graph(mcp, 'return await mcp.call("db","read",{value:1});'))
    assert not calls
    assert messages[0].status == "error"
    assert "disabled" in messages[0].content
    for code in (
        'return await __nova_mcp("not-json");',
        'return await __nova_mcp("x".repeat(100001));',
    ):
        messages = await execute(graph(mcp, code))
        assert messages[0].status == "error"


async def test_result_shapes_and_error_messages_are_bounded(tmp_path):
    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        return value

    mcp = inventory(tmp_path, read)
    messages = await execute(graph(mcp, 'return {__nova_code_error__: "ordinary data"};'))
    assert messages[0].status == "success"
    assert json.loads(messages[0].content)["result"] == {"__nova_code_error__": "ordinary data"}
    messages = await execute(graph(mcp, 'throw new Error("x".repeat(30000));'))
    assert messages[0].status == "error"
    assert len(messages[0].content) < 5000
    json.loads(messages[0].content)


async def test_script_and_memory_limits_fail_closed(tmp_path):
    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        return value

    mcp = inventory(tmp_path, read)
    messages = await execute(graph(mcp, "return 1;" + " " * 40000))
    assert messages[0].status == "error"
    messages = await execute(
        graph(mcp, 'return new Array(1000000).fill("data");', memory_limit=2 * 1024 * 1024)
    )
    assert messages[0].status == "error"
    assert any(word in messages[0].content.lower() for word in ("memory", "allocation"))


async def test_auto_approval_model_rejection_is_authoritative(tmp_path, monkeypatch):
    from novacode_cli.agents import approval_decisions
    from novacode_cli.config.nova_config import NovaConfig

    calls, decisions = [], []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    mcp = inventory(tmp_path, read)
    get_policy().tool_tiers["db_read"] = "ask"
    monkeypatch.setattr(NovaConfig, "get_auto_approve_decisions_enabled", lambda self: True)

    def reject(name, arguments, **kwargs):
        decisions.append((name, arguments))
        return {
            "decision": "reject",
            "source": "decision_model",
            "reason": "synthetic model denial",
        }

    monkeypatch.setattr(approval_decisions, "decide_tool_approval", reject)
    messages = await execute(
        graph(mcp, 'return await mcp.call("db","read",{value:7});', auto_approve=True)
    )
    assert decisions == [("db_read", {"value": 7})]
    assert not calls
    assert messages[0].status == "error"
    assert "synthetic model denial" in messages[0].content


@pytest.mark.parametrize("thread", [None, "owner", "other"])
async def test_existing_session_allow_is_respected(tmp_path, thread):
    from novacode_cli.security.rule_synthesis import synthesize_rule
    from novacode_cli.security.session_allow import get_session_allow
    from novacode_cli.session.runtime_identity import RuntimeIdentity, current_identity

    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Read a number."""
        calls.append(value)
        return value

    mcp = inventory(tmp_path, read)
    get_policy().tool_tiers["db_read"] = "ask"
    owner = RuntimeIdentity.from_config(str(tmp_path), {"configurable": {"thread_id": "owner"}}, "")
    identity = (
        RuntimeIdentity.from_config(str(tmp_path), {"configurable": {"thread_id": thread}}, "")
        if thread is not None
        else None
    )
    token = current_identity.set(identity)
    try:
        get_session_allow(owner.session if thread is not None else None).add(
            synthesize_rule("db_read", {"value": 7})
        )
        messages = await execute(graph(mcp, 'return await mcp.call("db","read",{value:7});'))
    finally:
        current_identity.reset(token)
    if thread == "other":
        assert messages[0].status == "error"
        assert "requires approval" in messages[0].content
        assert calls == []
    else:
        assert messages[0].status == "success", messages[0].content
        assert json.loads(messages[0].content)["result"] == 7
        assert calls == [7]


async def test_injected_runtime_has_unique_nested_call_id(tmp_path):
    @tool("db_read")
    def read(value: int, runtime: ToolRuntime) -> dict:
        """Return the injected call identity."""
        return {"value": value, "call": runtime.tool_call_id}

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            """
return await Promise.all([1,2].map(value=>mcp.call("db","read",{value})));
""",
        )
    )
    data = json.loads(messages[0].content)
    assert messages[0].status == "success", messages[0].content
    assert len({row["call"] for row in data["result"]}) == 2
    assert all(row["call"].startswith("mcp-code-") for row in data["result"])


def test_json_values_survive_script_result_roundtrip():
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from novacode_cli.mcp.code_runtime import run_script

    async def unavailable(raw):
        raise AssertionError("A pure expression must not call a tool")

    @given(
        st.lists(
            st.one_of(
                st.integers(min_value=-1_000_000, max_value=1_000_000),
                st.text(max_size=40),
                st.none(),
                st.booleans(),
            ),
            max_size=10,
        )
    )
    @settings(max_examples=12, deadline=None)
    def check(values):
        result, error = asyncio.run(
            run_script(
                "return " + json.dumps(values) + ";",
                unavailable,
                cpu_timeout=1,
                wall_timeout=5,
                memory_limit=16 * 1024 * 1024,
            )
        )
        assert error is None
        assert result == values

    check()


async def test_nested_interrupt_does_not_replay_prior_side_effects(tmp_path):
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    calls = []

    @tool("db_read")
    def read(value: int) -> int:
        """Record one synthetic side effect."""
        calls.append(value)
        return value

    class Approval(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "db_read" and request.tool_call["args"]["value"] == 2:
                raise GraphInterrupt((Interrupt(value={"reason": "nested approval required"}),))
            return await handler(request)

    messages = await execute(
        graph(
            inventory(tmp_path, read),
            """
await mcp.call("db","read",{value:1});
return await mcp.call("db","read",{value:2});
""",
            middleware=[Approval()],
        )
    )
    assert calls == [1]
    assert len(messages) == 1
    assert messages[0].status == "error"
    assert "approval" in messages[0].content.lower()


@pytest.mark.parametrize("slow", [False, True])
async def test_wrapped_mcp_errors_and_swallowed_cancellation_stay_errors(tmp_path, slow):
    from novacode_cli.mcp.middleware import _wrap_tool_error_handling

    @tool("db_read")
    async def read(value: int) -> int:
        """Simulate an actual MCP adapter failure."""
        if slow:
            await asyncio.sleep(20)
        raise RuntimeError("synthetic server error")

    mcp = inventory(tmp_path, _wrap_tool_error_handling(read))
    messages = await execute(
        graph(mcp, 'return await mcp.call("db","read",{value:1});', call_timeout=0.05)
    )
    assert messages[0].status == "error"
    assert ("timeout" if slow else "synthetic server error") in messages[0].content.lower()
