"""Todo reconciliation keeps verified completions and unresolved requirements visible."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.task_discipline import DONE_GATE_MARKER, TaskDisciplineMiddleware


def test_stale_todos_request_recorded_completion_without_redoing_work() -> None:
    out = TaskDisciplineMiddleware().after_model(
        {
            "messages": [HumanMessage(content="fix it"), AIMessage(content="Checks passed. Done!")],
            "todos": [{"content": "Fix and verify", "status": "in_progress"}],
        },
        None,
    )
    assert out["jump_to"] == "model"
    nudge = out["messages"][0].content
    assert "call `write_todos` to mark it completed" in nudge
    assert "a prose summary does not update the list" in nudge
    assert "Do not repeat completed work" in nudge
    assert "Keep blocked or unverified required items open" in nudge
    assert "do not mark them complete or delete them" in nudge
    assert "todos" not in out, "The reminder must not guess completion on the model's behalf"


async def test_done_gate_records_verified_statuses_and_preserves_blocked_work() -> None:
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class Model(GenericFakeChatModel):
        def bind_tools(self, *_args, **_kwargs) -> Model:  # noqa: ANN002, ANN003
            return self

    updated = [
        {"content": "Fix parse_date", "status": "completed"},
        {"content": "Live service check", "status": "pending"},
    ]
    initial = [dict(item, status="in_progress") for item in updated]
    model = Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "initial",
                            "name": "write_todos",
                            "args": {"todos": initial},
                        }
                    ],
                ),
                AIMessage(
                    content="The fix and local tests are done; live credentials are unavailable."
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "reconcile",
                            "name": "write_todos",
                            "args": {"todos": updated},
                        }
                    ],
                ),
                AIMessage(
                    content="Fix verified locally. Live service check is blocked on credentials."
                ),
            ]
        )
    )
    graph = create_agent(model, middleware=[TodoListMiddleware(), TaskDisciplineMiddleware()])
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="Fix and verify the issue")],
        }
    )
    assert result["todos"] == updated
    assert sum(isinstance(m, ToolMessage) for m in result["messages"]) == 2
    assert (
        sum(
            isinstance(m, HumanMessage) and str(m.content).startswith(DONE_GATE_MARKER)
            for m in result["messages"]
        )
        == 1
    )
    assert initial[0]["status"] == "in_progress"
