"""Run isolation, loading provenance and bounded disclosure for tool discovery."""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import numpy as np
import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from novacode_cli.agents.tool_search import (
    LOAD_RECORD,
    MAX_INVENTORY_CHARS,
    ToolSearchMiddleware,
    loaded_capabilities,
)
from novacode_cli.agents.tool_search_variants import BM25_TOOL, REGEX_TOOL
from novacode_cli.skills import retrieval
from tests.test_tool_search import _mcp, _request, task
from tests.test_tool_search_variants import database_records


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(retrieval, "_model", lambda: None)
    retrieval._index_cache.clear()
    yield
    retrieval._index_cache.clear()


def runtime(tools: list, call_id: str = "search", owner: str = "a") -> ToolRuntime:
    return ToolRuntime(
        state={"messages": [], "owner": owner},
        context=None,
        config={},
        stream_writer=lambda _: None,
        store=None,
        tools=tools,
        tool_call_id=call_id,
    )


def call(middleware: ToolSearchMiddleware, tools: list, query: str, name: str = "search_tools"):
    search = next(tool for tool in middleware.tools if tool.name == name)
    request = {"name": name, "id": "search", "type": "tool_call", "args": {"query": query}}
    response = search.invoke({**request, "args": {**request["args"], "runtime": runtime(tools)}})
    return [AIMessage("", tool_calls=[request]), response]


def test_model_requests_cannot_replace_another_runs_search_inventory():
    first = _mcp("run_a_action", "A-only operation.")
    second = _mcp("run_b_action", "B-only operation.")
    middleware = ToolSearchMiddleware()
    middleware._apply(_request([]).override(tools=[first]))
    middleware._apply(_request([]).override(tools=[second]))
    messages = call(middleware, [first], "*")
    assert ("tool", first.name) in loaded_capabilities(messages)
    assert second.name not in messages[-1].content


async def test_parallel_searches_resolve_private_tools_in_the_owning_context():
    ready = asyncio.Event()
    owners = []

    async def resolve(_messages: list, invocation: ToolRuntime) -> dict:
        owners.append(invocation.state["owner"])
        if len(owners) == 2:
            ready.set()
        await ready.wait()
        name = "private_" + invocation.state["owner"]
        return {name: _mcp(name, "Session-specific integration.")}

    middleware = ToolSearchMiddleware(skills=SimpleNamespace(_aresolve_tools=resolve))
    search = middleware.tools[0]

    async def run(owner: str) -> ToolMessage:
        return await search.ainvoke(
            {
                "name": search.name,
                "id": owner,
                "type": "tool_call",
                "args": {"query": "*", "runtime": runtime([], call_id=owner, owner=owner)},
            }
        )

    first, second = await asyncio.gather(run("a"), run("b"))
    assert "private_a" in first.content
    assert "private_b" not in first.content
    assert "private_b" in second.content
    assert "private_a" not in second.content


def test_zero_semantic_similarity_produces_no_loaded_results(monkeypatch: pytest.MonkeyPatch):
    class Model:
        def encode(self, texts: list[str]) -> np.ndarray:
            return np.array([[1.0, 0.0] if "quasarflute" in text else [0.0, 1.0] for text in texts])

    monkeypatch.setattr(retrieval, "_model", Model)
    messages = call(
        ToolSearchMiddleware(), [_mcp("browser_click", "Click an element.")], "quasarflute"
    )
    assert "No registered tools match" in messages[-1].content
    assert ("tool", "browser_click") not in loaded_capabilities(messages)


@pytest.mark.parametrize("name", ["search_tools", REGEX_TOOL, BM25_TOOL])
def test_every_mode_searches_parameter_descriptions(name: str):
    messages = call(ToolSearchMiddleware(), [database_records], "Solstice", name)
    assert ("tool", "database_records") in loaded_capabilities(messages)


@pytest.mark.parametrize("name", ["search_tools", BM25_TOOL])
def test_identifier_parameters_are_searchable_as_natural_words(name: str):
    from langchain_core.tools import tool

    @tool
    def opaque_operation(evergreen_record: str) -> str:
        """Perform a fixture operation."""
        return evergreen_record

    messages = call(ToolSearchMiddleware(), [opaque_operation], "evergreen record", name)
    assert ("tool", "opaque_operation") in loaded_capabilities(messages)


@pytest.mark.parametrize(
    "problem", ["orphan", "wrong_id", "wrong_name", "failed", "text_only", "version"]
)
def test_invalid_loading_evidence_cannot_bind_a_schema(problem: str):
    messages = call(
        ToolSearchMiddleware(), [_mcp("browser_click", "Click a page.")], "browser_click"
    )
    result = messages[-1]
    if problem == "orphan":
        messages = messages[-1:]
    elif problem == "wrong_id":
        result.tool_call_id = "other"
    elif problem == "wrong_name":
        result.name = "other"
    elif problem == "failed":
        result.status = "error"
    elif problem == "text_only":
        result.artifact = None
    else:
        result.artifact[LOAD_RECORD]["version"] = 99
    assert ("tool", "browser_click") not in loaded_capabilities(messages)


def test_tools_and_subagents_with_the_same_name_do_not_activate_each_other():
    tool = _mcp("reviewer", "Review source code.")
    middleware = ToolSearchMiddleware(deferred_subagents={"reviewer": "Review as a subagent."})
    search = middleware.tools[0]
    response = search.invoke(
        {
            "name": search.name,
            "id": "search",
            "type": "tool_call",
            "args": {"query": "reviewer", "limit": 1, "runtime": runtime([tool, task])},
        }
    )
    messages = [
        AIMessage("", tool_calls=[{"name": search.name, "args": {}, "id": "search"}]),
        response,
    ]
    assert ("tool", "reviewer") in loaded_capabilities(messages)
    assert ("subagent", "reviewer") not in loaded_capabilities(messages)
    bound = middleware._apply(_request(messages).override(tools=[tool, task])).tools
    assert "- reviewer:" not in next(t for t in bound if t.name == "task").description


@pytest.mark.parametrize("wire_format", ["tool", "function", "flat"])
def test_multiline_roster_removal_preserves_visible_agents_and_usage_notes(wire_format: str):
    description = "Hidden workflow.\n\nUse when:\n- Detailed private instructions"
    task_tool = _mcp(
        "task",
        (
            "Launch an agent.\n- hidden: " + description + "\n- visible: Public workflow.\n\n"
            "Specify subagent_type to select the agent. Usage notes:\n- Keep these instructions."
        ),
    )
    middleware = ToolSearchMiddleware(deferred_subagents={"hidden": description})
    wire = task_tool
    if wire_format == "function":
        wire = {
            "type": "function",
            "function": {"name": "task", "description": task_tool.description},
        }
    elif wire_format == "flat":
        wire = {"type": "function", "name": "task", "description": task_tool.description}
    result = middleware._apply(_request([]).override(tools=[wire])).tools[0]
    output = (
        result.get("function", result)["description"]
        if isinstance(result, dict)
        else result.description
    )
    assert "Detailed private instructions" not in output
    assert "Use when:" not in output
    assert "- visible: Public workflow." in output
    assert "Keep these instructions." in output
    assert "Detailed private instructions" in task_tool.description


def test_large_inventory_note_is_bounded_and_search_keeps_the_full_catalog():
    tools = [_mcp(f"service{i}_operation", "Optional operation.") for i in range(1000)]
    middleware = ToolSearchMiddleware()
    request = middleware._apply(_request([]).override(tools=tools))
    note = request.system_message.content_blocks[-1]["text"]
    assert len(note) <= MAX_INVENTORY_CHARS
    assert "1000 optional tools" in note
    assert "further tools" in note
    assert "service999_operation" not in note
    messages = call(middleware, tools, "service999_operation")
    assert ("tool", "service999_operation") in loaded_capabilities(messages)


def test_compaction_drops_discovered_schema_visibility_until_rediscovery():
    tool = _mcp("browser_click", "Click a page.")
    middleware = ToolSearchMiddleware()
    messages = call(middleware, [tool], tool.name)
    assert tool in middleware._apply(_request(messages).override(tools=[tool])).tools
    assert (
        tool
        not in middleware._apply(_request([HumanMessage("Continue")]).override(tools=[tool])).tools
    )


def test_runtime_is_not_a_provider_visible_argument():
    for tool in ToolSearchMiddleware().tools:
        assert "runtime" not in tool.tool_call_schema.model_json_schema()["properties"]


@pytest.mark.parametrize("version", [True, "1", None])
def test_malformed_versions_and_capabilities_do_not_load(*, version: bool | str | None):
    messages = call(ToolSearchMiddleware(), [_mcp("browser_click", "Click.")], "browser_click")
    messages[-1].artifact[LOAD_RECORD]["version"] = version
    assert ("tool", "browser_click") not in loaded_capabilities(messages)
    messages[-1].artifact[LOAD_RECORD] = {
        "version": 1,
        "capabilities": [
            None,
            {"kind": []},
            {"kind": "tool", "name": 3},
        ],
    }
    assert ("tool", "browser_click") not in loaded_capabilities(messages)


def test_failed_result_cannot_be_replayed_as_a_later_success():
    messages = call(ToolSearchMiddleware(), [_mcp("browser_click", "Click.")], "browser_click")
    good = messages[-1]
    failed = good.model_copy(update={"status": "error"})
    assert ("tool", "browser_click") not in loaded_capabilities([messages[0], failed, good])


def test_subagent_result_does_not_activate_a_same_named_tool():
    tool = _mcp("reviewer", "Review source code.")
    middleware = ToolSearchMiddleware(deferred_subagents={"reviewer": "Review as a subagent."})
    messages = call(middleware, [tool, task], "as a subagent", REGEX_TOOL)
    assert ("subagent", "reviewer") in loaded_capabilities(messages)
    assert ("tool", "reviewer") not in loaded_capabilities(messages)
    bound = middleware._apply(_request(messages).override(tools=[tool, task])).tools
    assert tool not in bound
    assert "- reviewer:" in next(t for t in bound if t.name == "task").description


async def test_search_schema_loading_does_not_bypass_a_downstream_denial():
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import tool

    from tests.test_skills_runtime import RecordingModel

    executions = []

    @tool
    def restricted_write() -> str:
        """Write a restricted fixture file."""
        executions.append("executed")
        return "Written"

    class Deny(AgentMiddleware):
        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
        ) -> ToolMessage:
            if request.tool_call["name"] == "restricted_write":
                return ToolMessage("Denied", tool_call_id=request.tool_call["id"], status="error")
            return await handler(request)

    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "search_tools", "id": "search", "args": {"query": "restricted_write"}}
                ],
            ),
            AIMessage("", tool_calls=[{"name": "restricted_write", "id": "write", "args": {}}]),
            AIMessage("Done"),
        ]
    )
    graph = create_agent(
        model, tools=[restricted_write], middleware=[Deny(), ToolSearchMiddleware()]
    )
    result = await graph.ainvoke({"messages": [HumanMessage("Find the fixture writing tool")]})
    assert "restricted_write" not in model.bound[0]
    assert "restricted_write" in model.bound[1]
    assert executions == []
    assert any(isinstance(m, ToolMessage) and m.status == "error" for m in result["messages"])


def test_structured_loading_survives_a_checkpoint_and_rebuilt_graph():
    from langchain.agents import create_agent
    from langgraph.checkpoint.memory import InMemorySaver

    from tests.test_skills_runtime import RecordingModel

    target = _mcp("rare_operation", "A rarely used fixture operation.")
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "search-checkpoint"}}
    first = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "search_tools",
                        "id": "search",
                        "args": {"query": target.name},
                    }
                ],
            ),
            AIMessage("Ready"),
        ]
    )
    create_agent(
        first, tools=[target], middleware=[ToolSearchMiddleware()], checkpointer=saver
    ).invoke(
        {"messages": [HumanMessage("Find the rare operation")]},
        config,
    )
    second = RecordingModel(
        responses=[
            AIMessage("", tool_calls=[{"name": target.name, "id": "run", "args": {"x": "value"}}]),
            AIMessage("Done"),
        ]
    )
    graph = create_agent(
        second, tools=[target], middleware=[ToolSearchMiddleware()], checkpointer=saver
    )
    result = graph.invoke({"messages": [HumanMessage("Continue")]}, config)
    assert target.name in second.bound[0]
    assert any(isinstance(m, ToolMessage) and m.content == "value" for m in result["messages"])
