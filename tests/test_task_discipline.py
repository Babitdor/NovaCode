"""Task discipline: todo recitation, the done gate, the protocol, empty results."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.agents import task_discipline as td
from novacode_cli.agents.task_discipline import DONE_GATE_MARKER, TaskDisciplineMiddleware

TODOS = [
    {"content": "Reproduce the bug", "status": "completed"},
    {"content": "Fix parse_date", "status": "in_progress"},
    {"content": "test_dates passes", "status": "pending"},
]


def _write_todos_call() -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": "w", "name": "write_todos", "args": {}}])


class _Request(SimpleNamespace):
    def override(self, **kw):  # noqa: ANN003, ANN201
        return _Request(**{**self.__dict__, **kw})


def _recited(messages: list, todos: list | None = TODOS) -> str:
    req = _Request(
        state={"todos": todos}, messages=messages, system_message=SystemMessage(content="BASE")
    )
    out = TaskDisciplineMiddleware().wrap_model_call(req, lambda r: r)
    return "".join(b.get("text", "") for b in out.system_message.content_blocks)


# ── recitation ─────────────────────────────────────────────────────────────


def test_plan_is_recited_once_it_scrolls_out_of_view() -> None:
    old = [_write_todos_call()] + [HumanMessage(content=str(i)) for i in range(td.RECITE_AFTER_MESSAGES)]
    text = _recited(old)
    assert text.startswith("BASE"), "the recitation must come AFTER the cached prompt"
    assert "[>] Fix parse_date" in text and "[ ] test_dates passes" in text


def test_plan_in_recent_view_is_not_repeated() -> None:
    """Re-showing it every call would re-bill the cached conversation."""
    assert _recited([HumanMessage(content="go"), _write_todos_call()]) == "BASE"


def test_nothing_to_recite_without_open_items() -> None:
    done = [dict(t, status="completed") for t in TODOS]
    assert _recited([HumanMessage(content="go")], todos=done) == "BASE"
    assert _recited([HumanMessage(content="go")], todos=None) == "BASE"


# ── done gate ──────────────────────────────────────────────────────────────


def _gate(messages: list, todos: list | None = TODOS):  # noqa: ANN202
    return TaskDisciplineMiddleware().after_model({"messages": messages, "todos": todos}, None)


def test_stopping_with_open_todos_sends_the_agent_back() -> None:
    out = _gate([HumanMessage(content="fix it"), AIMessage(content="Done!")])
    assert out["jump_to"] == "model"
    nudge = out["messages"][0].content
    assert nudge.startswith(DONE_GATE_MARKER) and "2 todo(s)" in nudge


def test_the_gate_fires_once_per_turn() -> None:
    history = [
        HumanMessage(content="fix it"),
        AIMessage(content="Done!"),
        HumanMessage(content=f"{DONE_GATE_MARKER} ..."),
        AIMessage(content="Blocked on credentials."),
    ]
    assert _gate(history) is None, "a real blocker must still be able to end the turn"
    # A new user turn re-arms it.
    assert _gate([*history, HumanMessage(content="next"), AIMessage(content="ok")]) is not None


def test_the_gate_ignores_work_in_progress_and_finished_lists() -> None:
    assert _gate([HumanMessage(content="x"), _write_todos_call()]) is None
    done = [dict(t, status="completed") for t in TODOS]
    assert _gate([HumanMessage(content="x"), AIMessage(content="Done")], todos=done) is None


def test_the_nudge_is_hidden_from_the_transcript() -> None:
    from novacode_cli.core.streaming import is_internal_context_text

    assert is_internal_context_text(DONE_GATE_MARKER + " You ended your turn")


def test_the_gate_really_jumps_back_in_an_agent_graph() -> None:
    """hook_config(can_jump_to) wiring, end to end through langchain's graph."""
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class _Model(GenericFakeChatModel):
        def bind_tools(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            return self

    plan = AIMessage(
        content="",
        tool_calls=[{"id": "w1", "name": "write_todos", "args": {"todos": TODOS}}],
    )
    model = _Model(
        messages=iter([plan, AIMessage(content="Done!"), AIMessage(content="Finished all.")])
    )
    # As in Nova's stack, `todos` enters state only through the write_todos tool.
    agent = create_agent(model, middleware=[TodoListMiddleware(), TaskDisciplineMiddleware()])
    result = agent.invoke({"messages": [HumanMessage(content="fix it")]})
    contents = [m.content for m in result["messages"]]
    assert any(c.startswith(DONE_GATE_MARKER) for c in contents), contents
    assert contents[-1] == "Finished all."


# ── the protocol in the prompt ──────────────────────────────────────────────


def test_todos_no_longer_stall_for_approval() -> None:
    from novacode_cli.prompts import render_template

    text = render_template("core_agent_system.jinja")
    assert "without stopping for approval" in text
    assert "3+ steps → write todos, present plan, **wait for approval**" not in text
    assert "Understand → Change → Verify" in text
    for size in ("Trivial", "Standard", "Large or risky"):
        assert size in text


# ── empty tool results ──────────────────────────────────────────────────────


def test_empty_tool_results_are_filled_before_the_model_sees_them() -> None:
    from novacode_cli.agents.tool_call_repair import EMPTY_TOOL_RESULT, RepairToolCallsEachStep

    call = AIMessage(content="", tool_calls=[{"id": "c", "name": "serena_read_file", "args": {}}])
    empty = ToolMessage(content=[{"type": "text", "text": ""}], tool_call_id="c", id="t")
    out = RepairToolCallsEachStep().before_model({"messages": [HumanMessage(content="x"), call, empty]}, None)
    assert out["messages"][-1].content == EMPTY_TOOL_RESULT
    assert out["messages"][-1].tool_call_id == "c"


def test_a_healthy_history_is_left_alone() -> None:
    from novacode_cli.agents.tool_call_repair import RepairToolCallsEachStep

    call = AIMessage(content="", tool_calls=[{"id": "c", "name": "ls", "args": {}}])
    ok = ToolMessage(content="a.py", tool_call_id="c")
    assert RepairToolCallsEachStep().before_model({"messages": [call, ok]}, None) is None
