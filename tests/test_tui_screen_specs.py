"""The pure screen builders: plain numbers in, blocks and a spec out.

@@/context@@ is the first command that opens a modal, so these tests pin the two
things a screenshot cannot: the numbers are the tracker's own, and the spec is
shaped the way @@novacode_cli/ui/port.ScreenSpec@@ documents so the ratatui
renderer needs no per-command code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novacode_cli.ui.screen_specs import (
    ContextUsage,
    context_blocks,
    context_spec,
    context_usage,
)

if TYPE_CHECKING:
    from novacode_cli.ui.port import ScreenSpec


class _Breakdown:
    """A breakdown object shaped like @@TokenTracker.get_breakdown()@@."""

    def __init__(self, **over: Any) -> None:
        self.context_window_size = 200_000
        self.total_tokens = 1_000
        self.system_prompt_tokens = 400
        self.user_memory_tokens = 100
        self.project_memory_tokens = 0
        self.tool_definitions_tokens = 200
        self.user_message_tokens = 100
        self.assistant_message_tokens = 100
        self.tool_result_tokens = 100
        self.mcp_tool_tokens = 0
        self.mcp_prompt_tokens = 0
        self.mcp_tool_count = 0
        self.user_message_count = 2
        self.assistant_message_count = 2
        self.tool_call_count = 1
        for key, value in over.items():
            setattr(self, key, value)


class _Tracker:
    """A tracker stand-in: only the attributes the report reads."""

    def __init__(self, breakdown: _Breakdown | None = None, **over: Any) -> None:
        self.model_name = "test-model"
        self.has_api_data = False
        self.session_total_tokens = 1_555
        self.last_output = 321
        self._breakdown = breakdown
        for key, value in over.items():
            setattr(self, key, value)

    def get_breakdown(self) -> _Breakdown | None:
        return self._breakdown


def _usage(**over: Any) -> ContextUsage:
    base: dict[str, Any] = {
        "model": "test-model",
        "used": 1_000,
        "window": 200_000,
        "categories": (("System + memory", 500), ("Tool results", 500)),
        "session_total": 1_555,
        "last_output": 321,
        "user_messages": 2,
        "assistant_messages": 2,
        "tool_calls": 1,
    }
    base.update(over)
    return ContextUsage(**base)


def test_percent_and_remaining_are_derived_from_the_window() -> None:
    usage = _usage()
    assert usage.percent == 0.5
    assert usage.remaining == 199_000


def test_an_unknown_window_reports_zero_percent_and_never_negative() -> None:
    usage = _usage(used=0, window=0)
    assert usage.percent == 0.0
    assert usage.remaining == 0
    assert _usage(used=300_000).remaining == 0


def test_usage_reads_every_fact_from_the_tracker() -> None:
    usage = context_usage(_Tracker(_Breakdown()), "fallback-model")
    assert usage is not None
    assert usage.model == "test-model"
    assert usage.used == 1_000
    assert usage.window == 200_000
    assert usage.session_total == 1_555
    assert usage.last_output == 321
    assert usage.user_messages == 2
    assert usage.tool_calls == 1
    assert usage.exact is False
    # `System + memory` adds the two memory counters to the system prompt.
    assert usage.categories[0] == ("System + memory", 500)
    assert [label for label, _ in usage.categories] == [
        "System + memory",
        "Tool definitions",
        "User messages",
        "Assistant messages",
        "Tool results",
    ]


def test_mcp_is_reported_as_its_own_row_only_when_present() -> None:
    breakdown = _Breakdown(mcp_tool_tokens=200, mcp_prompt_tokens=100, mcp_tool_count=2)
    usage = context_usage(_Tracker(breakdown))
    assert usage is not None
    assert usage.categories[-1] == ("  of which MCP", 300)


def test_no_tracker_and_no_breakdown_are_both_the_empty_state() -> None:
    assert context_usage(None) is None
    assert context_usage(_Tracker(None)) is None


def test_a_failing_breakdown_does_not_raise() -> None:
    class _Broken:
        def get_breakdown(self) -> None:
            msg = "boom"
            raise RuntimeError(msg)

    assert context_usage(_Broken()) is None


def test_blocks_report_the_hero_the_metrics_and_the_counts() -> None:
    text = context_blocks(_usage())[0].text
    assert "test-model" in text
    assert "0.5%" in text
    assert "1,000 / 200,000 tokens" in text
    assert "199,000 remaining" in text
    assert "Estimated" in text
    assert "Session total 1,555 tokens" in text
    assert "2 user · 2 assistant · 1 tool calls" in text


def test_blocks_say_when_the_numbers_are_provider_reported() -> None:
    text = context_blocks(_usage(exact=True))[0].text
    assert "Provider-reported" in text


def test_blocks_handle_the_empty_state() -> None:
    assert context_blocks(None)[0].text == "No token usage captured yet."


def test_spec_is_a_table_with_one_row_per_category() -> None:
    spec = context_spec(_usage())
    assert spec.kind == "table"
    assert spec.title == "Context window"
    payload = spec.payload
    assert [column["key"] for column in payload["columns"]] == ["category", "tokens", "share"]
    rows = payload["rows"]
    assert len(rows) == 2
    assert rows[0]["category"] == "System + memory"
    assert rows[0]["tokens"] == "500"
    assert rows[0]["share"] == "50.0%"
    assert rows[0]["id"] != rows[1]["id"], "each row needs a stable id to answer with"
    assert json_round_trips(spec)


def test_spec_is_an_empty_table_without_usage() -> None:
    spec = context_spec(None)
    assert spec.payload["rows"] == []
    assert json_round_trips(spec)


def json_round_trips(spec: ScreenSpec) -> bool:
    """Report whether a spec survives the wire, as the frame encoder requires."""
    import json

    encoded = json.dumps({"kind": spec.kind, "title": spec.title, "payload": spec.payload})
    return json.loads(encoded)["kind"] == spec.kind
