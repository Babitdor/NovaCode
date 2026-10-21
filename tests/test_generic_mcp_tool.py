"""Generic calls preserve routing, schemas, policy and MCP session semantics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from novacode_cli.mcp.generic import MCPInput, bounded_result, build_mcp_tool
from novacode_cli.mcp.middleware import MCPMiddleware, _fetch_raw_tools
from novacode_cli.security.delegated_approval import HardDenyMiddleware
from novacode_cli.security.policy import load_policy


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, *args, **kwargs):
        return self


def make_middleware(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers": {}}')
    middleware = MCPMiddleware(config)
    middleware._tools_discovered = True
    return middleware


def register(middleware, server, raw, target):
    target.metadata = {"nova_mcp_server": server}
    middleware.tools.append(target)
    middleware._tools_cache.append(
        {
            "server": server,
            "skill_alias": raw,
            "name": target.name,
            "description": target.description,
        }
    )


@pytest.mark.asyncio
async def test_generic_calls_run_through_the_agent_graph_and_downstream_policy(
    tmp_path, monkeypatch
):
    mw = make_middleware(tmp_path)
    executed = []
    observed = []

    @tool("db_read")
    def read(value: int) -> str:
        """Read a typed value."""
        executed.append(value)
        return json.dumps({"rows": [{"value": value}], "large": "x" * 10000})

    register(mw, "db", "read", read)
    generic = build_mcp_tool(mw)
    policy = load_policy(tmp_path)
    policy.tool_tiers["db_read"] = "allow"
    monkeypatch.setattr("novacode_cli.security.delegated_approval.get_policy", lambda: policy)

    class Observe(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            observed.append(request.tool_call["name"])
            return await handler(request)

    def graph(value=7):
        model = ScriptedModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "MCPTool",
                                "id": "m1",
                                "args": {
                                    "action": "call",
                                    "server_name": "db",
                                    "tool_name": "read",
                                    "arguments": {"value": value},
                                    "result_path": ["rows", 0, "value"],
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        )
        return create_agent(
            model, tools=[generic], middleware=[mw, Observe(), HardDenyMiddleware()]
        )

    result = await graph().ainvoke({"messages": [HumanMessage(content="read")]})
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert executed == [7]
    assert observed == ["db_read"]
    assert message.name == "MCPTool" and message.tool_call_id == "m1"
    assert json.loads(message.content) == {"result": "7", "truncated": False}

    invalid = await graph(value="not an integer").ainvoke(
        {"messages": [HumanMessage(content="read")]}
    )
    message = next(m for m in invalid["messages"] if isinstance(m, ToolMessage))
    assert message.status == "error"
    assert executed == [7], "generic routing bypassed the target's argument schema"

    policy.tool_tiers["db_read"] = "deny"
    result = await graph().ainvoke({"messages": [HumanMessage(content="read")]})
    assert executed == [7], "generic routing bypassed a target deny"
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert message.status == "error" and "denied" in message.content


def test_discovery_describes_required_schema_and_never_executes(tmp_path):
    mw = make_middleware(tmp_path)

    @tool("one_read")
    def read(value: int) -> str:
        """Read from server one."""
        raise AssertionError("Discovery executed a tool")

    register(mw, "one", "read", read)
    wrapper = build_mcp_tool(mw)
    assert wrapper.invoke({"action": "list"})["tools"][0]["server_name"] == "one"
    schema = wrapper.invoke({"action": "describe", "server_name": "one", "tool_name": "read"})[
        "schema"
    ]
    assert schema["parameters"]["required"] == ["value"]
    assert "error" in wrapper.invoke(
        {"action": "describe", "server_name": "two", "tool_name": "read"}
    )
    assert "error" in wrapper.invoke({"action": "call", "server_name": "one", "tool_name": "read"})


@pytest.mark.asyncio
async def test_stdio_discovery_allowlist_reused_state_and_structured_selection(
    tmp_path, monkeypatch
):
    from novacode_cli.agents.strict_tools import StrictToolMiddleware

    monkeypatch.setenv("NOVA_STRICT_TOOLS", "auto")
    strict = StrictToolMiddleware()
    config = tmp_path / "mcp.json"
    fixture = Path(__file__).parent / "fixtures" / "generic_mcp_stdio.py"
    connection = {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]}
    config.write_text(json.dumps({"mcpServers": {"peer": {**connection, "tools": ["increment"]}}}))
    mw = MCPMiddleware(config)
    raw = await _fetch_raw_tools(connection)
    mw.tools = mw._convert_raw_tools("peer", connection, raw)
    mw._tools_discovered = True
    generic = build_mcp_tool(mw)
    assert [e["tool_name"] for e in generic.invoke({"action": "list"})["tools"]] == ["increment"]

    async def execute(request):
        return await strict.awrap_tool_call(request, lambda r: r.tool.ainvoke(r.tool_call))

    try:
        for expected in (2, 4):
            call = {
                "type": "tool_call",
                "name": "MCPTool",
                "id": "call",
                "args": {
                    "action": "call",
                    "server_name": "peer",
                    "tool_name": "increment",
                    "arguments": {"amount": 2},
                    "result_path": ["count"],
                },
            }
            result = await mw.awrap_tool_call(ToolCallRequest(call, generic, {}, None), execute)
            assert result.status == "success", result.content
            assert json.loads(result.content)["result"] == str(expected)
        call["args"]["arguments"] = {"amount": "2"}
        invalid = await mw.awrap_tool_call(ToolCallRequest(call, generic, {}, None), execute)
        assert invalid.status == "error" and "Invalid tool arguments" in invalid.content
        call["args"]["arguments"] = {"amount": 2}
        valid = await mw.awrap_tool_call(ToolCallRequest(call, generic, {}, None), execute)
        assert json.loads(valid.content)["result"] == "6", "Invalid arguments reached the server"
        call["args"]["tool_name"] = "forbidden"
        result = await mw.awrap_tool_call(ToolCallRequest(call, generic, {}, None), execute)
        assert result.status == "error" and "disabled" in result.content
    finally:
        await mw._session_pool.aclose()


def test_payload_truncation():
    message = ToolMessage(content="x" * 10000, tool_call_id="id")
    result = bounded_result(message, MCPInput(max_output_chars=128))
    assert json.loads(result.content) == {"result": "x" * 128, "truncated": True}


def test_server_names_with_underscores_cannot_route_to_another_server(tmp_path):
    mw = make_middleware(tmp_path)

    @tool("a_b_read")
    def first() -> str:
        """Read on server a_b."""
        return "first"

    @tool("a_b_read")
    def second() -> str:
        """Read b_read on server a."""
        return "second"

    register(mw, "a_b", "read", first)
    register(mw, "a", "b_read", second)
    for server, raw, expected in (("a_b", "read", "first"), ("a", "b_read", "second")):
        call = {
            "type": "tool_call",
            "name": "MCPTool",
            "id": "id",
            "args": {
                "action": "call",
                "server_name": server,
                "tool_name": raw,
            },
        }
        result = mw.wrap_tool_call(
            ToolCallRequest(call, build_mcp_tool(mw), {}, None),
            lambda r: r.tool.invoke(r.tool_call),
        )
        assert json.loads(result.content)["result"] == expected


def test_sync_route_handles_bad_paths_and_preserves_explicit_router_deny(tmp_path, monkeypatch):
    mw = make_middleware(tmp_path)
    executed = []

    @tool("db_read")
    def read() -> str:
        """Return a structured response."""
        executed.append(True)
        return '{"found": 1}'

    register(mw, "db", "read", read)
    call = {
        "type": "tool_call",
        "id": "id",
        "name": "MCPTool",
        "args": {
            "action": "call",
            "server_name": "db",
            "tool_name": "read",
            "result_path": ["missing"],
        },
    }
    request = ToolCallRequest(call, build_mcp_tool(mw), {}, None)
    result = mw.wrap_tool_call(request, lambda r: r.tool.invoke(r.tool_call))
    assert result.status == "error" and "selection failed" in result.content
    policy = load_policy(tmp_path)
    policy.tool_tiers["MCPTool"] = "deny"
    monkeypatch.setattr("novacode_cli.security.policy.get_policy", lambda: policy)
    result = mw.wrap_tool_call(request, lambda r: r.tool.invoke(r.tool_call))
    assert result.status == "error" and "denied" in result.content
    assert executed == [True]


def test_policy_follows_the_target_and_gates_mutations(tmp_path):
    from novacode_cli.hitl.interrupts import get_interrupt_configs

    policy = load_policy(tmp_path)
    policy.tool_tiers["db_drop"] = "deny"
    assert policy.evaluate("MCPTool", {"action": "list"}).tier == "allow"
    assert policy.evaluate("MCPTool", {"action": []}).tier == "deny"
    assert (
        policy.evaluate(
            "MCPTool", {"action": "call", "server_name": "db", "tool_name": "drop"}
        ).tier
        == "deny"
    )
    for name in ("MCPTool", "CronCreateTool", "CronDeleteTool"):
        assert name in get_interrupt_configs()
