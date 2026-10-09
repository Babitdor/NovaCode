"""Restored context must be bounded, readable, and isolated from old checkpoints."""

from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from novacode_cli.context.history import effective_messages
from novacode_cli.session.context_restore import seed_restored_context
from novacode_cli.session.session_persistence import SessionData, SessionMeta
from novacode_cli.session.session_prompt_builder import build_continuation_prompt
from novacode_cli.tui.app import NovaApp
from novacode_cli.ui.ui_elements import TokenTracker


def session(messages):
    return SessionData(meta=SessionMeta(
        session_id="saved", thread_id="old-thread", created_at="2026-10-10",
        last_active="2026-10-10", project_root=None, repo_hash=None,
        Nova_md_checksum=None, model_name="gpt-4o", assistant_id="nova-agent",
    ), messages=messages)


@pytest.mark.asyncio
async def test_restore_does_not_merge_old_history_or_summarization_cutoff():
    class State(TypedDict):
        messages: Annotated[list, add_messages]
        _summarization_event: dict

    graph = StateGraph(State)
    graph.add_node("model", lambda state: {})
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    agent = graph.compile(checkpointer=InMemorySaver())
    old_config = {"configurable": {"thread_id": "old-thread"}}
    old = HumanMessage("archived history", id="old")
    await agent.aupdate_state(old_config, {
        "messages": [old],
        "_summarization_event": {"cutoff_index": 20, "summary_message": HumanMessage("old summary")},
    }, as_node="model")
    restored = [SystemMessage("continuation"), HumanMessage("retained question")]
    first = await seed_restored_context(agent, restored, as_node="model")
    second = await seed_restored_context(agent, restored, as_node="model")
    assert first != second and first != "old-thread"
    state = await agent.aget_state({"configurable": {"thread_id": second}})
    assert [m.content for m in effective_messages(state.values)] == [m.content for m in restored]
    assert not state.values.get("_summarization_event")
    old_state = await agent.aget_state(old_config)
    assert old_state.values["messages"][0].content == old.content


def test_restore_budget_counts_large_tool_arguments_and_repairs_pairs():
    messages = [
        HumanMessage("previous question"),
        AIMessage("", tool_calls=[{"name": "write_file", "args": {"content": "x" * 100_000}, "id": "t"}]),
        ToolMessage("written", tool_call_id="t"),
        HumanMessage("latest question"),
    ]
    restored = build_continuation_prompt(session(messages), "instructions")
    assert isinstance(restored[0], SystemMessage)
    assert [m.content for m in restored[1:]] == ["latest question"]
    assert len(messages) == 4  # The saved transcript remains intact.


def test_restore_budget_counts_retained_reasoning():
    restored = build_continuation_prompt(session([
        AIMessage("answer", additional_kwargs={"reasoning_content": "x" * 100_000}),
        HumanMessage("latest question"),
    ]), "instructions")
    assert [m.content for m in restored[1:]] == ["latest question"]


@pytest.mark.asyncio
async def test_restored_metrics_count_continuation_and_injected_baseline():
    tracker = TokenTracker()
    tracker.set_model("gpt-4o")
    tracker.set_baseline(400)
    messages = [SystemMessage("restored briefing " * 100), HumanMessage("retained question " * 100)]
    agent = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={"messages": messages})))
    app = SimpleNamespace(
        token_tracker=tracker,
        session_state=SimpleNamespace(thread_id="restored", _tools=[]),
        _active_agent=lambda: (agent, None), _refresh_router_model_from_state=lambda values: None,
        _routed_model_name=None, model_name="gpt-4o", _maybe_warn_ollama_offload=AsyncMock(),
    )
    tracker.reset()
    await NovaApp._update_context_breakdown(app)
    breakdown = tracker.get_breakdown()
    assert breakdown.system_prompt_tokens > 400
    assert breakdown.user_message_tokens > 0
    assert breakdown.total_tokens == breakdown.baseline_tokens + breakdown.conversation_tokens
    assert not tracker.has_api_data  # Old request totals must not masquerade as restored usage.
