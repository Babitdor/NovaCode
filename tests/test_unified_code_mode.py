"""Exercise the primary interpreter surface, persistence and live approvals."""
# ruff: noqa: ANN001 -- graph fixtures and scripted-model inputs are heterogeneous.

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime  # noqa: TC002 -- tool schema inspects this annotation.
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.agents.tool_search import ToolSearchMiddleware
from novacode_cli.core.code_approvals import CodeApprovals, current_code_approvals
from novacode_cli.security.policy import get_policy
from tests.test_mcp_code_mode import ScriptedModel, inventory, isolated_policy  # noqa: F401

if TYPE_CHECKING:
    from novacode_cli.security.policy import ApprovalDecision

pytestmark = pytest.mark.timeout(40)


def scripted(snippets, *, middleware=(), tools=(), checkpointer=None):
    messages = []
    for name, code in snippets:
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": f"eval_{len(messages)}", "name": name, "args": {"code": code}}
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    return create_agent(
        ScriptedModel(messages=iter(messages)),
        tools=list(tools),
        middleware=list(middleware),
        checkpointer=checkpointer,
    )


async def invoke(graph, thread="chat"):
    output = await graph.ainvoke(
        {"messages": [HumanMessage(content="Run the workflow")]},
        {"configurable": {"thread_id": thread}},
    )
    return [m for m in output["messages"] if isinstance(m, ToolMessage)][-1]


async def test_variables_survive_snippets_turns_and_compatibility_but_not_other_chats():
    mode = CodeModeMiddleware()
    graph = scripted(
        [
            ("eval", "let kept = 41; kept"),
            ("eval", "kept += 1; kept"),
            ("MCPCode", "return kept;"),
            ("eval", "typeof kept"),
        ],
        middleware=[mode],
        checkpointer=InMemorySaver(),
    )
    assert json.loads((await invoke(graph)).content)["result"] == "41"
    assert json.loads((await invoke(graph)).content)["result"] == "42"
    assert json.loads((await invoke(graph)).content)["result"] == 42
    assert json.loads((await invoke(graph, "new-chat")).content)["result"] == "undefined"


async def test_host_bridge_survives_multiple_snippets_in_one_turn():
    calls = []

    @tool
    def echo(value: int) -> int:
        """Return the current invocation's value."""
        calls.append(value)
        return value

    get_policy().tool_tiers["echo"] = "allow"
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"snippet-{value}",
                    "name": "eval",
                    "args": {"code": f"await tools.call('echo', {{value:{value}}});"},
                }
            ],
        )
        for value in (1, 2)
    ]
    graph = create_agent(
        ScriptedModel(messages=iter([*messages, AIMessage(content="done")])),
        tools=[echo],
        middleware=[CodeModeMiddleware()],
    )
    output = await graph.ainvoke({"messages": [HumanMessage(content="Use two snippets")]})
    results = [m for m in output["messages"] if isinstance(m, ToolMessage)]
    assert all(m.status == "success" for m in results), results
    assert [json.loads(m.content)["result"] for m in results] == ["1", "2"]
    assert calls == [1, 2]


async def test_truncated_reads_can_be_inspected_without_repeating_host_calls():
    calls = []

    @tool
    def read_file(file_path: str) -> str:
        """Return source text."""
        calls.append(file_path)
        return "SOURCE" * 3000

    get_policy().tool_tiers["read_file"] = "allow"
    graph = scripted(
        [
            (
                "eval",
                "var source = await tools.call('read_file', {file_path:'/pkg/graph.py'}); source",
            ),
            ("eval", "source.slice(-12)"),
        ],
        tools=[read_file],
        middleware=[CodeModeMiddleware()],
        checkpointer=InMemorySaver(),
    )
    first = json.loads((await invoke(graph)).content)
    assert first["truncated"] is True
    assert "incomplete" in first["notice"]
    assert json.loads((await invoke(graph)).content)["result"] == "SOURCESOURCE"
    assert calls == ["/pkg/graph.py"]


async def test_markdown_syntax_failure_is_safe_and_quoted_lines_can_be_written():
    writes = []

    @tool
    def write_file(file_path: str, content: str) -> str:
        """Save a guide."""
        writes.append((file_path, content))
        return "saved"

    get_policy().tool_tiers["write_file"] = "allow"
    lines = ["# Guide", "Read `pkg/graph.py`.", "Windows path: B:\\project"]
    graph = scripted(
        [
            ("eval", "const bad = `Read `pkg/graph.py` here`;"),
            (
                "eval",
                "await tools.call('write_file', {file_path:'/.nova/onboard/guide.md', "
                f"content:{json.dumps(lines)}.join('\\n')}})",
            ),
        ],
        tools=[write_file],
        middleware=[CodeModeMiddleware()],
    )
    failure = json.loads((await invoke(graph)).content)
    assert "SyntaxError" in failure["error"]
    assert failure["calls"] == 0
    assert "no host tool calls" in failure["hint"]
    assert writes == []
    assert (await invoke(graph)).status == "success"
    assert writes == [("/.nova/onboard/guide.md", "\n".join(lines))]


@pytest.mark.parametrize("name", ["read_file", "edit_file"])
async def test_filesystem_error_text_cannot_be_reported_as_a_successful_update(name):
    @tool(name)
    def failing_file(file_path: str) -> str:
        """Return the backend's missing-file error."""
        return f"Error: File '{file_path}' not found"

    get_policy().tool_tiers[name] = "allow"
    graph = scripted(
        [("eval", f"await tools.call('{name}', {{file_path:'/nnovaagents/graph.py'}}); 'updated'")],
        tools=[failing_file],
        middleware=[CodeModeMiddleware()],
    )
    result = await invoke(graph)
    body = json.loads(result.content)
    assert result.status == "error"
    assert body["failed_calls"] == 1
    assert "exact virtual path" in body["error"]
    assert "result" not in body


async def test_runtime_syntax_error_does_not_claim_an_earlier_write_was_skipped():
    writes = []

    @tool
    def write_file(file_path: str) -> str:
        """Record an external write."""
        writes.append(file_path)
        return "saved"

    get_policy().tool_tiers["write_file"] = "allow"
    graph = scripted(
        [
            (
                "eval",
                "await tools.call('write_file', {file_path:'/guide.md'}); "
                "throw new SyntaxError('runtime error after write');",
            )
        ],
        tools=[write_file],
        middleware=[CodeModeMiddleware()],
    )
    body = json.loads((await invoke(graph)).content)
    assert body["calls"] == 1
    assert "not rolled back" in body["hint"]
    assert "no host tool calls" not in body["hint"]
    assert writes == ["/guide.md"]


def test_media_surface_does_not_advertise_ordinary_text_reads_or_mutate_host_tool():
    from novacode_cli.agents.code_mode import code_mode_surface

    @tool
    def read_file(file_path: str) -> str:
        """Read ordinary source files."""
        return file_path

    original = read_file.description
    for host in (read_file, {"name": "read_file", "description": original}):
        visible = code_mode_surface([host])[0]
        description = visible["description"] if isinstance(visible, dict) else visible.description
        assert description.startswith("Media fallback only")
        assert "await tools.call('read_file'" in description
        assert (host["description"] if isinstance(host, dict) else host.description) == original


async def test_parallel_interpreter_calls_share_state_without_collisions():
    calls = []
    active = maximum = 0

    @tool
    async def echo(value: int, runtime: ToolRuntime) -> int:
        """Record a bounded host operation for the current snippet."""
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        calls.append((value, runtime.tool_call_id))
        active -= 1
        return value

    get_policy().tool_tiers["echo"] = "allow"
    code = "globalThis.counter = (globalThis.counter ?? 0) + 1; "
    code += "await tools.call('echo', {value: counter}); "
    tool_calls = [
        {
            "id": f"snippet-{i}",
            "name": name,
            "args": {"code": code + ("return counter;" if name == "MCPCode" else "counter;")},
        }
        for i, name in enumerate(("eval", "MCPCode", "eval"))
    ]
    graph = create_agent(
        ScriptedModel(
            messages=iter([AIMessage(content="", tool_calls=tool_calls), AIMessage(content="done")])
        ),
        tools=[echo],
        middleware=[CodeModeMiddleware()],
    )
    output = await graph.ainvoke({"messages": [HumanMessage(content="Run three snippets")]})
    results = [m for m in output["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == 3
    assert all(m.status == "success" for m in results), results
    assert sorted(int(json.loads(m.content)["result"]) for m in results) == [1, 2, 3]
    assert [value for value, _ in calls] == [1, 2, 3]
    assert len({call_id for _, call_id in calls}) == 3
    assert maximum == 1


async def test_cancelling_queued_snippet_does_not_evict_running_interpreter(monkeypatch):
    from unittest.mock import AsyncMock

    from langchain.agents.middleware.types import ToolCallRequest

    mode = CodeModeMiddleware()
    started, release = asyncio.Event(), asyncio.Event()
    executions = []

    async def execute(request, _handler, code) -> ToolMessage:
        executions.append(code)
        started.set()
        await release.wait()
        return mode._message(request, code, error=False)

    async def handler(request) -> ToolMessage:
        return await request.tool.coroutine(**request.tool_call["args"])

    monkeypatch.setattr(mode, "_execute", execute)
    evict = AsyncMock()
    monkeypatch.setattr(mode._registry, "aevict", evict)

    def request(code) -> ToolCallRequest:
        return ToolCallRequest(
            tool_call={"id": code, "name": "eval", "args": {"code": code}},
            tool=mode.tools[0],
            state={},
            runtime=None,
        )

    running = asyncio.create_task(mode.awrap_tool_call(request("running"), handler))
    await started.wait()
    queued_entered = asyncio.Event()
    lock = mode._execution_locks[mode._fallback_thread_id]
    acquire = lock.acquire

    async def record_waiter() -> bool:
        queued_entered.set()
        return await acquire()

    monkeypatch.setattr(lock, "acquire", record_waiter)
    queued = asyncio.create_task(mode.awrap_tool_call(request("queued"), handler))
    await queued_entered.wait()
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    evict.assert_not_called()
    release.set()
    assert (await running).content == "running"
    assert executions == ["running"]
    assert (await mode.awrap_tool_call(request("later"), handler)).content == "later"


async def test_different_conversations_keep_independent_interpreter_queues():
    started = []
    release = asyncio.Event()

    @tool
    async def echo(runtime: ToolRuntime) -> str:
        """Wait until both conversations have entered their own interpreter."""
        thread = runtime.config["configurable"]["thread_id"]
        started.append(thread)
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 3)
        return thread

    get_policy().tool_tiers["echo"] = "allow"
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "snippet",
                "name": "eval",
                "args": {"code": "globalThis.marker = await tools.call('echo'); marker;"},
            }
        ],
    )
    model = ScriptedModel(
        messages=iter([call, call, AIMessage(content="done"), AIMessage(content="done")])
    )
    graph = create_agent(
        model, tools=[echo], middleware=[CodeModeMiddleware()], checkpointer=InMemorySaver()
    )
    results = await asyncio.gather(invoke(graph, "first"), invoke(graph, "second"))
    assert all(result.status == "success" for result in results), results
    assert [json.loads(result.content)["result"] for result in results] == ["first", "second"]
    assert set(started) == {"first", "second"}


async def test_real_deepagents_task_uses_unified_interpreter():
    from deepagents import create_deep_agent

    child = ScriptedModel(messages=iter([AIMessage(content="CHILD_RESULT")]))
    parent = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "eval",
                            "name": "eval",
                            "args": {
                                "code": "await task({description:'Review',"
                                "subagentType:'reviewer'});"
                            },
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    get_policy().tool_tiers["task"] = "allow"
    graph = create_deep_agent(
        model=parent,
        subagents=[
            {
                "name": "reviewer",
                "description": "Review code",
                "system_prompt": "Review the request and return your result.",
                "model": child,
            }
        ],
        middleware=[CodeModeMiddleware()],
    )
    result = await invoke(graph)
    assert result.status == "success", result.content
    assert json.loads(result.content)["result"] == "CHILD_RESULT"


async def test_composed_native_mcp_parallel_delegation_keeps_intermediates_private(tmp_path):
    dispatched = []
    active = maximum = 0

    @tool
    def inputs() -> list:
        """Discover workflow inputs."""
        return [1, 2, 3]

    @tool("db_read")
    def read(value: int) -> dict:
        """Read one input."""
        return {"value": value, "private": "INTERMEDIATE_ONLY"}

    @tool("task")
    async def delegate(description: str, subagent_type: str, runtime: ToolRuntime) -> Command:
        """Configured specialists: reviewer."""
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        dispatched.append((description, subagent_type))
        active -= 1
        return Command(
            update={
                "files": {f"/{description}": {"content": [description]}},
                "messages": [ToolMessage(content=description, tool_call_id=runtime.tool_call_id)],
            }
        )

    for name in ("inputs", "task"):
        get_policy().tool_tiers[name] = "allow"
    mcp = inventory(tmp_path, read)
    graph = scripted(
        [
            (
                "eval",
                """
var catalog = await tools.list();
var schema = await tools.describe('inputs');
var remote = await mcp.list();
var values = await tools.call('inputs');
var records = await Promise.all(values.map(value => mcp.call('db','read',{value})));
var selected = records.filter(row => row.value > 1);
var children = await Promise.all(selected.map(row =>
  task({description:String(row.value),subagentType:'reviewer'})));
children.join(',');
""",
            )
        ],
        tools=[inputs, delegate],
        middleware=[CodeModeMiddleware(mcp, concurrency=2), mcp],
    )
    output = await graph.ainvoke({"messages": [HumanMessage(content="Compose the workflow")]})
    assert "INTERMEDIATE_ONLY" not in repr(output["messages"])
    assert json.loads(output["messages"][-2].content)["result"] == "2,3"
    assert dispatched == [("2", "reviewer"), ("3", "reviewer")]
    assert maximum == 2


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ({"type": "approve"}, [1, 2]),
        ({"type": "reject"}, [1]),
        ({"type": "edit", "edited_action": {"name": "second", "args": {"value": 9}}}, [1, 9]),
        ({"type": "edit", "edited_action": {"name": "first", "args": {"value": 9}}}, [1]),
        ({"type": "edit", "edited_action": {"name": "second", "args": {"value": "9"}}}, [1]),
        ({"type": "approve", "revoke": True}, [1]),
    ],
)
async def test_approval_suspends_live_script_without_replaying_first_write(decision, expected):
    calls = []

    @tool
    def first(value: int) -> int:
        """First allowed side effect."""
        calls.append(value)
        return value

    @tool
    def second(value: int) -> int:
        """Second side effect requires its own approval."""
        calls.append(value)
        return value

    get_policy().tool_tiers.update(first="allow", second="ask")
    graph = scripted(
        [("eval", "await tools.call('first',{value:1}); await tools.call('second',{value:2});")],
        tools=[first, second],
        middleware=[CodeModeMiddleware(cpu_timeout=0.05, wall_timeout=1)],
    )
    broker = CodeApprovals()
    token = current_code_approvals.set(broker)
    try:
        async for kind, data in graph.astream(
            {"messages": [HumanMessage(content="Run writes")]}, stream_mode=["custom", "updates"]
        ):
            if kind == "custom" and data.get("type") == "nova_code_approval":
                assert calls == [1]
                assert len(broker.pending) == 1
                await asyncio.sleep(1.2)  # Human wait exceeds both execution budgets.
                if decision.get("revoke"):
                    get_policy().tool_tiers["second"] = "deny"
                broker.pending[data["id"]][1].set_result({"decisions": [decision]})
        assert calls == expected
        assert not broker.pending
    finally:
        broker.close()
        current_code_approvals.reset(token)


async def test_cancel_pending_approval_rejects_late_decisions():
    broker = CodeApprovals()
    emitted = asyncio.Queue()
    pending = asyncio.create_task(broker.request(emitted.put_nowait, {"action_requests": []}))
    event = await emitted.get()
    future = broker.pending[event["id"]][1]
    broker.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert future.cancelled()
    assert not broker.pending
    with pytest.raises(ValueError, match="active approval handler"):
        await broker.request(emitted.put_nowait, {})


@pytest.mark.parametrize("reset", [False, True])
async def test_real_shared_loop_resumes_or_cancels_exact_pending_write(reset):
    from novacode_cli import ui_events as ev
    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.states.Session import SessionState
    from tests.test_agent_invocation import ScriptedModel as StreamingModel

    calls = []

    @tool
    def write(value: int) -> int:
        """Record a permitted or approved side effect."""
        calls.append(value)
        return value

    # The first call is authorized separately; the second needs a user decision.
    get_policy().tool_tiers["write"] = "ask"
    original = get_policy().evaluate

    def evaluate(name: str, args: dict) -> ApprovalDecision:
        if name == "write" and args.get("value") == 1:
            from novacode_cli.security.policy import ApprovalDecision

            return ApprovalDecision("allow")
        return original(name, args)

    get_policy().evaluate = evaluate
    model = StreamingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "eval-live",
                        "name": "eval",
                        "args": {
                            "code": "await tools.call('write',{value:1}); "
                            "await tools.call('write',{value:2});"
                        },
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(
        model,
        tools=[write],
        middleware=[CodeModeMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = SessionState()
    session.auto_approve = False
    prompts = []
    events = []
    async for event in iterate_agent_events("Run writes", graph, "nova-agent", session):
        events.append(event)
        if isinstance(event, ev.InterruptRequest):
            prompts.append(event)
            assert calls == [1]
            if reset:
                session.reset_conversation()
                assert event.future.cancelled()
            else:
                event.future.set_result({"decisions": [{"type": "approve"}]})
    assert len(prompts) == 1, events
    assert not any(isinstance(event, ev.Error) for event in events), events
    assert calls == ([1] if reset else [1, 2])


async def test_restore_failure_is_reported_and_state_is_cleared(monkeypatch):
    mode = CodeModeMiddleware()
    repl = mode._registry.get(mode._fallback_thread_id)

    async def fail(*_args: object, **_kwargs: object) -> None:
        message = "Broken snapshot"
        raise ValueError(message)

    monkeypatch.setattr(repl, "arestore_snapshot", fail)
    update = await mode.abefore_agent({"_quickjs_snapshot_payload": b"invalid"}, None)
    assert update == {"_quickjs_snapshot_payload": None}
    graph = scripted([("eval", "typeof kept")], middleware=[mode])
    # Use the stable fallback to address the failed-restoration conversation.
    result = await graph.ainvoke({"messages": [HumanMessage(content="Check state")]})
    body = json.loads(result["messages"][-2].content)
    assert "restoration failed" in body["state_warning"]
    assert body["result"] == "undefined"


async def test_explicit_agent_route_selects_eval_only_while_enabled():
    from langchain.agents.middleware.types import ModelRequest

    from novacode_cli.agents.invocation import DELEGATION_PREFIX, forced_tool
    from novacode_cli.config.nova_config import NovaConfig

    prompt = DELEGATION_PREFIX + "- @reviewer: `task(subagent_type='reviewer', description=...)`."
    request = ModelRequest(
        model=ScriptedModel(messages=iter([])),
        messages=[HumanMessage(content=prompt)],
        tools=[{"name": "eval"}, {"name": "task"}],
    )
    assert forced_tool(request) == "eval"
    NovaConfig().set_code_mode_enabled(False)
    assert forced_tool(request) == "task"


@pytest.mark.parametrize("entry", ["eval", "MCPCode"])
async def test_canonical_interpreter_denial_also_blocks_compatibility_entry(entry):
    get_policy().tool_tiers["eval"] = "deny"
    graph = scripted(
        [(entry, "throw new Error('SCRIPT_EXECUTED');")], middleware=[CodeModeMiddleware()]
    )
    result = await invoke(graph)
    assert result.status == "error"
    assert "denied" in result.content
    assert "SCRIPT_EXECUTED" not in result.content


@pytest.mark.parametrize("mutation", ["routing", "state", "messages"])
async def test_delegation_rejects_unsupported_graph_mutations(mutation):
    @tool("task")
    def delegate(description: str, subagent_type: str, runtime: ToolRuntime) -> Command:  # noqa: ARG001 -- task schema.
        """A configured child returning unsupported graph updates."""
        messages = [ToolMessage(content="CHILD_TRANSCRIPT", tool_call_id=runtime.tool_call_id)]
        if mutation == "routing":
            return Command(goto="other", update={"messages": messages})
        if mutation == "state":
            return Command(update={"unowned": "CHILD_TRANSCRIPT", "messages": messages})
        return Command(update={"messages": [AIMessage(content="CHILD_TRANSCRIPT"), *messages]})

    get_policy().tool_tiers["task"] = "allow"
    graph = scripted(
        [("eval", "await task({description:'Review',subagentType:'reviewer'});")],
        tools=[delegate],
        middleware=[CodeModeMiddleware()],
    )
    result = await invoke(graph)
    assert result.status == "error"
    assert "unsupported" in result.content
    assert "CHILD_TRANSCRIPT" not in result.content


async def test_delegation_requests_and_validates_structured_response():
    @tool("task")
    def delegate(description: str, subagent_type: str, runtime: ToolRuntime) -> str:  # noqa: ARG001 -- task schema.
        """A configured child with structured output support."""
        from deepagents.middleware.subagents import SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY

        assert SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY in runtime.config["configurable"]
        return '{"answer":42}'

    get_policy().tool_tiers["task"] = "allow"
    graph = scripted(
        [
            (
                "eval",
                """
var result = await task({description:'Review',subagentType:'reviewer',responseSchema:{
  type:'object',properties:{answer:{type:'integer'}},required:['answer']
}});
result.answer;
""",
            )
        ],
        tools=[delegate],
        middleware=[CodeModeMiddleware()],
    )
    result = await invoke(graph)
    assert result.status == "success", result.content
    assert json.loads(result.content)["result"] == "42"


@pytest.mark.parametrize("provider", ["anthropic", "openai", "portable"])
def test_search_and_provider_fallback_cannot_restore_ordinary_schemas(provider):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    @tool
    def shell(command: str) -> str:
        """Execute a command."""
        return command

    @tool
    def read_file(file_path: str) -> str:
        """Read ordinary source files."""
        return file_path

    mode = CodeModeMiddleware()
    search = ToolSearchMiddleware()
    search._frequent = {"shell"}
    model = FakeListChatModel(
        responses=["ok"], metadata={"nova_provider": provider, "nova_model": "test"}
    )
    request = ModelRequest(
        model=model, state={}, messages=[], tools=[*mode.tools, shell, read_file, *search.tools]
    )
    for portable in (False, True):
        if portable:
            search._portable_routes.add(search._route_key(request))
        bound = search._apply(mode._model_request(request, enabled=True))
        names = {t.name if hasattr(t, "name") else t.get("name") for t in bound.tools}
        assert "eval" in names
        assert "shell" not in names
        media = next(t for t in bound.tools if getattr(t, "name", None) == "read_file")
        assert media.description.startswith("Media fallback only")
        assert "primary control surface" in bound.system_message.text
