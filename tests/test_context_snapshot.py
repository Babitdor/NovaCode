"""Regression coverage for the TUI's context snapshot, without a live provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.tui.app import NovaApp
from novacode_cli.ui.execution import _bound_tools
from novacode_cli.ui.ui_elements import TokenTracker


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_checkpoint", [False, True])
async def test_empty_checkpoint_keeps_tools_and_system_baseline(missing_checkpoint):
    tracker = TokenTracker()
    tracker.set_model("claude-opus-4-8")
    tracker.set_baseline(400)
    agent = SimpleNamespace(
        aget_state=AsyncMock(return_value=None if missing_checkpoint else SimpleNamespace(values={})),
    )
    app = SimpleNamespace(
        token_tracker=tracker,
        session_state=SimpleNamespace(thread_id="destination-thread", _tools=[
            {"name": "read_file", "description": "Read a workspace file", "parameters": {}},
        ]),
        _active_agent=lambda: (agent, None),
        _refresh_router_model_from_state=lambda values: None,
        _routed_model_name=None,
        model_name="claude-opus-4-8",
        _maybe_warn_ollama_offload=AsyncMock(),
    )
    await NovaApp._update_context_breakdown(app)
    breakdown = tracker.get_breakdown()
    assert breakdown.system_prompt_tokens == 400
    assert breakdown.tool_definitions_tokens > 0
    assert breakdown.total_tokens == breakdown.baseline_tokens
    assert breakdown.user_message_tokens == 0
    agent.aget_state.assert_awaited_once_with({"configurable": {"thread_id": "destination-thread"}})


@pytest.mark.asyncio
async def test_compaction_snapshot_counts_only_summary_and_retained_tail():
    tracker = TokenTracker()
    tracker.set_model("claude-opus-4-8")
    tracker.set_baseline(400)
    messages = [
        HumanMessage("archived " * 10_000),
        HumanMessage("retained user " * 100),
        AIMessage("retained assistant " * 100),
        ToolMessage("retained result " * 100, tool_call_id="t"),
    ]
    agent = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={
        "messages": messages,
        "_summarization_event": {"cutoff_index": 1, "summary_message": HumanMessage("summary " * 100)},
    })))
    app = SimpleNamespace(
        token_tracker=tracker,
        session_state=SimpleNamespace(thread_id="compacted-thread", _tools=[]),
        _active_agent=lambda: (agent, None),
        _refresh_router_model_from_state=lambda values: None,
        _routed_model_name=None,
        model_name="claude-opus-4-8",
        _maybe_warn_ollama_offload=AsyncMock(),
    )
    tracker.reset()
    await NovaApp._update_context_breakdown(app)
    breakdown = tracker.get_breakdown()
    assert breakdown.user_message_count == 2
    assert 0 < breakdown.user_message_tokens < 2000
    assert breakdown.assistant_message_tokens > 0
    assert breakdown.tool_result_tokens > 0
    assert breakdown.total_tokens == breakdown.baseline_tokens + breakdown.conversation_tokens


def test_tool_schemas_are_available_from_a_real_compiled_agent():
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    agent = create_deep_agent(model=FakeListChatModel(responses=["done"]))
    tools = _bound_tools(agent)
    assert tools
    assert "read_file" in {tool.name for tool in tools}
