"""Real graph regression for accidental parallel planning calls in Code Mode."""

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.todos import NovaTodoListMiddleware

pytestmark = pytest.mark.timeout(30)


class Model(GenericFakeChatModel):
    def bind_tools(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return self


def call(identity: str, *tasks: dict):
    return {"id": identity, "name": "write_todos", "args": {"todos": list(tasks)}}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_parallel_updates_keep_distinct_tasks_and_last_status(*, asynchronous: bool):
    first = {"content": "Investigate", "status": "in_progress"}
    done = {"content": "Investigate", "status": "completed"}
    next_task = {"content": "Test", "status": "pending"}
    model = Model(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[call("a", first), call("b", done, next_task)]),
                AIMessage(content="Plan recorded"),
            ]
        )
    )
    from novacode_cli.agents.code_mode import CodeModeMiddleware

    graph = create_agent(model, middleware=[CodeModeMiddleware(), NovaTodoListMiddleware()])
    initial = {"messages": [HumanMessage(content="Fix and test")]}
    result = await graph.ainvoke(initial) if asynchronous else graph.invoke(initial)
    assert result["todos"] == [done, next_task]
    results = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == 1
    assert results[0].tool_call_id == "b"
    assert results[0].status == "success"


def test_single_snapshot_and_invalid_batch_keep_upstream_semantics():
    middleware = NovaTodoListMiddleware()
    assert (
        middleware.after_model({"messages": [AIMessage(content="", tool_calls=[call("a")])]}, None)
        is None
    )
    invalid = call("b", {"content": "Task", "status": "invalid"})
    update = middleware.after_model(
        {"messages": [AIMessage(content="", tool_calls=[call("a"), invalid])]}, None
    )
    assert all(m.status == "error" for m in update["messages"])
