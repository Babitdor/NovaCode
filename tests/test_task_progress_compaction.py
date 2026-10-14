"""Compaction carries evidence of completed work into the next model decision."""

# ruff: noqa: ANN401, ARG002

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

import novacode_cli.compaction as compact
from novacode_cli.agents.context_compaction import ContextCompactionMiddleware, limits_for
from novacode_cli.context.task_progress import task_progress


def _call(name: str, ident: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": ident, "args": {"step": 1}}])


def test_progress_resets_for_new_request_and_rejects_stale_todos() -> None:
    old = [
        HumanMessage(content="Build A"),
        _call("work", "old"),
        ToolMessage(content="A written", tool_call_id="old"),
    ]
    first = task_progress(old)
    assert first["completed_actions"][0]["outcome"] == "returned"
    assert first["next_action"] == "unknown"
    summary = HumanMessage(
        content="summary",
        additional_kwargs={"lc_source": "summarization", "nova_task_progress": first},
    )
    fresh = task_progress(
        [summary, HumanMessage(content="Build B")], [{"content": "Finish A", "status": "pending"}]
    )
    assert fresh["task"] == "Build B"
    assert fresh["completed_actions"] == []
    assert fresh["next_action"] == "unknown"


def test_reported_todos_and_repeated_compaction_stay_bounded() -> None:
    messages = [
        HumanMessage(content="Build A"),
        AIMessage(
            content="", tool_calls=[{"name": "write_todos", "id": "todo", "args": {"todos": []}}]
        ),
        ToolMessage(content="ok", tool_call_id="todo"),
    ]
    todos = [
        {"content": "Write A", "status": "completed"},
        {"content": "Test A", "status": "in_progress"},
    ]
    record = task_progress(messages, todos)
    assert record["reported_completed_todos"] == ["Write A"]
    assert record["next_action"] == "Test A"
    interrupted = task_progress(
        [
            *messages,
            _call("work", "written"),
            ToolMessage(content="A written", tool_call_id="written"),
        ],
        todos,
    )
    assert interrupted["completed_actions"][0]["tool"] == "work"
    assert interrupted["next_action"] == "Test A"
    summary = HumanMessage(
        content="summary",
        additional_kwargs={"lc_source": "summarization", "nova_task_progress": record},
    )
    repeated = task_progress([summary])
    assert repeated == record
    many = [summary]
    for index in range(20):
        ident = f"c{index}"
        many.extend([_call("work", ident), ToolMessage(content="ok", tool_call_id=ident)])
    assert len(task_progress(many)["completed_actions"]) == 4
    assert task_progress(many, todos)["next_action"] == "unknown"


def test_failed_action_remains_retryable() -> None:
    messages = [
        HumanMessage(content="Fetch data"),
        _call("work", "failed"),
        ToolMessage(content="temporary error", tool_call_id="failed", status="error"),
    ]
    record = task_progress(messages)
    assert record["completed_actions"][0]["outcome"] == "error"
    assert record["next_action"] == "unknown"


@tool
def work(step: int) -> str:
    """Perform a numbered task step."""
    return f"step {step} finished"


class ReplayModel(BaseChatModel):
    """Choose the next action from the actual request sent through the graph."""

    seen: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "progress-replay"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ReplayModel:
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        if run_manager and (run_manager.metadata or {}).get("lc_source") == "summarization":
            reply = AIMessage(content="Earlier work summarized.")
        else:
            self.seen.append(messages)
            summary = next(
                (
                    m
                    for m in messages
                    if isinstance(m, HumanMessage) and m.additional_kwargs.get("nova_task_progress")
                ),
                None,
            )
            if summary is None:
                message = "context_length_exceeded: request too long"
                raise ValueError(message)
            progress = summary.additional_kwargs["nova_task_progress"]
            step = 2 if any(a["tool"] == "work" for a in progress["completed_actions"]) else 1
            reply = (
                AIMessage(content="done")
                if len(self.seen) > 2
                else AIMessage(
                    content="",
                    tool_calls=[{"name": "work", "id": f"next-{step}", "args": {"step": step}}],
                )
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])


def test_graph_replay_chooses_unfinished_action_after_compaction() -> None:
    model = ReplayModel()
    middleware = ContextCompactionMiddleware(model, context_window=4_000)
    agent = create_agent(model, tools=[work], middleware=[middleware], checkpointer=InMemorySaver())
    history = [
        HumanMessage(content="Do step 1, then step 2"),
        _call("work", "done-1"),
        ToolMessage(content="step 1 finished", tool_call_id="done-1"),
        AIMessage(content="notes " * 1800),
    ]
    config = {"configurable": {"thread_id": "progress-replay"}, "recursion_limit": 4}
    result = agent.invoke({"messages": history}, config)
    assert model.seen
    assert any(
        isinstance(message, AIMessage)
        and any(call["args"].get("step") == 2 for call in message.tool_calls)
        for message in result["messages"]
    )


def test_live_message_contains_record_and_manual_uses_same_builder() -> None:
    middleware = ContextCompactionMiddleware(context_window=4_000)
    head = [
        HumanMessage(content="Do step 1, then step 2"),
        _call("work", "one"),
        ToolMessage(content="finished", tool_call_id="one"),
    ]
    message = middleware._summary_message(head, "summary", None, limits_for(4_000))
    assert message.additional_kwargs["nova_task_progress"] == task_progress(head)
    assert '"next_action":"unknown"' in str(message.content)


class ManualAgent:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.updates: list[dict[str, Any]] = []

    async def aget_state(self, config: Any) -> Any:
        return type("State", (), {"values": {"messages": self.messages}})()

    async def aupdate_state(self, *, config: Any, values: dict[str, Any], as_node: str) -> None:
        self.updates.append(values)


class ManualModel:
    model_name = "manual-test"

    async def ainvoke(self, messages: Any) -> AIMessage:
        return AIMessage(content="summary")


def test_manual_compaction_publishes_progress_only_after_archive(
    monkeypatch: Any,
) -> None:
    history = [
        HumanMessage(content="Do step 1"),
        _call("work", "one"),
        ToolMessage(content="finished", tool_call_id="one"),
    ]
    agent = ManualAgent(history)
    monkeypatch.setattr(compact, "_archive_messages", lambda *_args: None)
    failed = asyncio.run(compact.compact_conversation(agent, ManualModel(), "manual"))
    assert not failed.success
    assert agent.updates == []

    monkeypatch.setattr(compact, "_archive_messages", lambda *_args: "archive.jsonl")
    finished = asyncio.run(compact.compact_conversation(agent, ManualModel(), "manual"))
    assert finished.success
    summary = agent.updates[0]["messages"][1]
    assert summary.additional_kwargs["nova_task_progress"]["completed_actions"][0]["tool"] == "work"
