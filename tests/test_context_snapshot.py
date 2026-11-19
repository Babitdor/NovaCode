"""Regression coverage for the TUI's context snapshot, without a live provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli import ui_events as ev
from novacode_cli.tui.app import NovaApp
from novacode_cli.ui.execution import _bound_tools
from novacode_cli.ui.ui_elements import TokenTracker


@pytest.fixture(autouse=True)
def _private_home(monkeypatch, tmp_path):
    """/context remembers the last prompt size in ~/.nova; keep tests off the real one."""
    from novacode_cli.config import config

    monkeypatch.setattr(config, "HOME_DIR", tmp_path)



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
        _read_state=lambda ag, config, **_: ag.aget_state(config),
        _context_strategies=NovaApp._context_strategies,
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
        _read_state=lambda ag, config, **_: ag.aget_state(config),
        _context_strategies=NovaApp._context_strategies,
    )
    tracker.reset()
    await NovaApp._update_context_breakdown(app)
    breakdown = tracker.get_breakdown()
    assert breakdown.user_message_count == 2
    assert 0 < breakdown.user_message_tokens < 2000
    assert breakdown.assistant_message_tokens > 0
    assert breakdown.tool_result_tokens > 0
    assert breakdown.total_tokens == breakdown.baseline_tokens + breakdown.conversation_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_count", "live_count", "expected", "exact"),
    [
        (8000, None, 8000, True),
        (None, None, 7400, False),
        (8000, 9300, 9300, True),
        (None, 2000, 2000, True),
    ],
)
async def test_context_refresh_uses_latest_request(provider_count, live_count, expected, exact):
    tracker = TokenTracker()
    tracker.model_name = "gpt-4o"
    tracker.add(2000, 40)
    paints = []
    snapshot = {
        "generation": "next",
        "estimated_input_tokens": 7400,
        "estimated_system_tokens": 0,
        "provider_input_tokens": provider_count,
    }

    async def read_state(_agent, _config):
        if live_count is not None:
            await NovaApp._render(app, ev.ContextUsage(live_count))
        return SimpleNamespace(values={
            "_context_snapshot": snapshot,
            "messages": [HumanMessage("inspect the workspace")],
        })

    app = SimpleNamespace(
        token_tracker=tracker,
        session_state=SimpleNamespace(thread_id="code-mode-thread"),
        _active_agent=lambda: (None, None),
        _read_state=read_state,
        _refresh_router_model_from_state=lambda _: None,
        _context_strategies=NovaApp._context_strategies,
        _routed_model_name=None,
        model_name="gpt-4o",
        _maybe_warn_ollama_offload=AsyncMock(),
        _refresh_usage_metrics=lambda: paints.append(tracker.get_breakdown().total_tokens),
    )
    await NovaApp._update_context_breakdown(app)

    assert tracker.current_context == expected
    assert tracker.get_breakdown().total_tokens == expected
    assert tracker.has_api_data is exact
    assert tracker.session_total_tokens == 2040
    assert paints[-1] == expected

    await NovaApp._render(app, ev.UsageUpdate(
        input_tokens=2000, output_tokens=40, context_reported=True,
    ))
    assert tracker.get_breakdown().total_tokens == expected
    assert tracker.session_total_tokens == 4080


def test_tool_schemas_are_available_from_a_real_compiled_agent():
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    agent = create_deep_agent(model=FakeListChatModel(responses=["done"]))
    tools = _bound_tools(agent)
    assert tools
    assert "read_file" in {tool.name for tool in tools}
