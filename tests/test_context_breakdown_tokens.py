"""Regression tests for context-usage token accounting.

Focus: tool-call arguments (file contents, diffs, JSON) live in
``AIMessage.tool_calls`` — not ``.content`` — and were previously counted as
zero, badly under-reporting context usage and starving the compaction heuristic.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.context._analysis import (
    ContextBreakdown,
    _message_text,
    _tool_call_text,
    build_context_breakdown,
    get_context_window_size,
)

MODEL = "claude-opus-4-8"


def test_provider_scaling_categories_sum_exactly_without_mutating_estimate():
    breakdown = ContextBreakdown(
        system_prompt_tokens=1,
        user_memory_tokens=1,
        project_memory_tokens=1,
        tool_definitions_tokens=1,
        user_message_tokens=1,
        assistant_message_tokens=1,
        tool_result_tokens=1,
        total_tokens=7,
    )
    for total in (1, 3, 10, 1000, 100003):
        scaled = breakdown.scaled_to(total)
        assert scaled.total_tokens == total
        assert scaled.baseline_tokens + scaled.conversation_tokens == total
    assert breakdown.total_tokens == 7


def test_tool_call_args_are_counted():
    big = "X" * 4000  # ~1000 tokens at 4 chars/token
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "write_file", "args": {"path": "/a", "content": big}, "id": "t1"}],
    )
    bd = build_context_breakdown([ai], MODEL)
    # Previously 0 — the args weren't in .content so they vanished.
    assert bd.assistant_message_tokens >= 900
    assert bd.tool_call_count == 1
    assert bd.total_tokens >= 900


def test_text_content_still_counted():
    ai = AIMessage(content="hello " * 100)  # ~150 tokens
    bd = build_context_breakdown([ai], MODEL)
    assert bd.assistant_message_tokens > 0


def test_tool_call_text_serializes_name_and_args():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "edit", "args": {"old": "a", "new": "b"}, "id": "x"}],
    )
    out = _tool_call_text(ai)
    assert "edit" in out and "old" in out and "new" in out


def test_message_text_reads_tool_result_blocks():
    tm = ToolMessage(
        content=[{"type": "tool_result", "content": [{"type": "text", "text": "Y" * 2000}]}],
        tool_call_id="t1",
    )
    assert len(_message_text(tm)) >= 2000


def test_current_models_have_exact_windows():
    assert get_context_window_size("claude-opus-4-8") == 200_000
    assert get_context_window_size("claude-sonnet-4-8") == 200_000
    # Unknown claude variants still fall back to 200K, never the 128K default.
    assert get_context_window_size("claude-something-new") == 200_000


def test_full_conversation_totals_add_up():
    msgs = [
        SystemMessage(content="S" * 400),  # ~100
        HumanMessage(content="H" * 400),  # ~100
        AIMessage(
            content="A" * 400,  # ~100
            tool_calls=[{"name": "shell", "args": {"command": "C" * 4000}, "id": "t"}],
        ),
        ToolMessage(content="R" * 800, tool_call_id="t"),  # ~200
    ]
    bd = build_context_breakdown(msgs, MODEL)
    # system + user + assistant(text+toolargs) + tool_result
    assert bd.total_tokens == (
        bd.system_prompt_tokens
        + bd.user_message_tokens
        + bd.assistant_message_tokens
        + bd.tool_result_tokens
    )
    assert bd.assistant_message_tokens >= 1000  # text (~100) + tool args (~1000)


def test_empty_conversation_still_counts_tool_definitions():
    """Opening /context before a user turn must not report an empty baseline."""
    bd = build_context_breakdown(
        [], MODEL, tools=[{"name": "read_file", "description": "Read a file", "parameters": {}}]
    )
    assert bd.tool_definitions_tokens > 0
    assert bd.total_tokens == bd.tool_definitions_tokens
