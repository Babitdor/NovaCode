"""Fallback token capture from the persisted final AIMessage.

Some providers (notably Ollama) don't surface ``usage_metadata`` on the final
streamed chunk, so ``execute_task``'s primary capture path stays at zero.  The
``_capture_fallback_usage`` helper reads the aggregated AIMessage from the
persisted graph state instead.  These tests pin that behaviour and its
best-effort error handling.
"""

from __future__ import annotations

from types import SimpleNamespace

from novacode_cli.ui.execution import _capture_fallback_usage
from novacode_cli.ui.ui_elements import TokenTracker


class _FakeAgent:
    """Minimal agent exposing ``aget_state`` over a fixed message list."""

    def __init__(self, messages: list[object], *, raise_exc: Exception | None = None) -> None:
        self._messages = messages
        self._raise_exc = raise_exc

    async def aget_state(self, _config: object) -> SimpleNamespace:
        if self._raise_exc is not None:
            raise self._raise_exc
        return SimpleNamespace(values={"messages": self._messages})


def _msg(usage: dict | None) -> SimpleNamespace:
    return SimpleNamespace(usage_metadata=usage)


async def test_fallback_captures_usage_from_final_message():
    tracker = TokenTracker()
    agent = _FakeAgent([_msg({"input_tokens": 1234, "output_tokens": 56})])

    await _capture_fallback_usage(agent, {}, tracker)

    assert tracker.current_context == 1234
    assert tracker.last_output == 56
    assert tracker.has_api_data is True


async def test_fallback_uses_most_recent_message_with_usage():
    tracker = TokenTracker()
    # Newest message (last in list) carries usage; an earlier one differs.
    agent = _FakeAgent(
        [
            _msg({"input_tokens": 10, "output_tokens": 1}),
            _msg(None),  # e.g. a tool message with no usage
            _msg({"input_tokens": 999, "output_tokens": 42}),
        ]
    )

    await _capture_fallback_usage(agent, {}, tracker)

    assert tracker.current_context == 999
    assert tracker.last_output == 42


async def test_fallback_skips_zero_usage_but_stops_scanning():
    tracker = TokenTracker()
    agent = _FakeAgent([_msg({"input_tokens": 0, "output_tokens": 0})])

    await _capture_fallback_usage(agent, {}, tracker)

    # Zero usage must not flip has_api_data — nothing real was captured.
    assert tracker.has_api_data is False
    assert tracker.current_context == 0


async def test_fallback_no_message_with_usage_is_noop():
    tracker = TokenTracker()
    agent = _FakeAgent([_msg(None), _msg(None)])

    await _capture_fallback_usage(agent, {}, tracker)

    assert tracker.has_api_data is False


async def test_fallback_swallows_aget_state_errors():
    tracker = TokenTracker()
    agent = _FakeAgent([], raise_exc=RuntimeError("graph exploded"))

    # Must not raise — a learning/telemetry failure can't break the turn.
    await _capture_fallback_usage(agent, {}, tracker)

    assert tracker.has_api_data is False


def test_session_total_accumulates_and_survives_reset():
    """The 'usage' meter sums input+output per turn, climbs monotonically, and
    survives /compact (reset) but is zeroed by /clear (reset_session=True)."""
    t = TokenTracker()
    assert t.session_total_tokens == 0

    t.add(1000, 200)
    t.add(1500, 300)
    assert t.session_total_tokens == 3000  # (1000+200) + (1500+300)

    t.reset()  # /compact
    assert t.session_total_tokens == 3000  # survives compaction

    t.add(500, 100)
    assert t.session_total_tokens == 3600  # keeps climbing afterwards

    t.reset(reset_session=True)  # /clear — true fresh start
    assert t.session_total_tokens == 0  # usage meter zeroed

    t.add(200, 50)
    assert t.session_total_tokens == 250  # restarts from zero


def test_live_window_supersedes_stale_startup_window():
    """The per-turn live-detected window (e.g. Ollama ps after the model loads)
    must reach get_breakdown(), not get clobbered by the window captured once at
    set_model() time."""
    from novacode_cli.context import ContextBreakdown

    t = TokenTracker()
    t.context_window_size = 200_000  # captured at startup (model not yet loaded)
    t.add(50_000, 1_000)  # has_api_data=True, current_context=50_000

    # Per-turn breakdown detects the real window live (e.g. cloud model = 1M).
    t.set_breakdown(ContextBreakdown(context_window_size=1_048_576))

    bd = t.get_breakdown()
    assert bd.context_window_size == 1_048_576  # live wins, not the stale 200k
    assert bd.total_tokens == 50_000  # API total override still applies
    assert t.context_window_size == 1_048_576  # stored copy refreshed for direct readers


def test_session_pct_is_fraction_of_budget():
    t = TokenTracker()
    t.session_token_budget = 10_000
    assert t.session_pct == 0.0
    t.add(2000, 500)  # 2500 / 10_000
    assert t.session_pct == 25.0
    t.session_token_budget = 0  # guard: no divide-by-zero
    assert t.session_pct == 0.0
def test_session_usage_can_include_multiple_calls():
    from novacode_cli.ui.ui_elements import TokenTracker

    tracker = TokenTracker()
    tracker.add(500, 40, session_tokens=2570)
    assert tracker.current_context == 500
    assert tracker.session_total_tokens == 2570
    tracker.reset()
    assert tracker.session_total_tokens == 2570


def test_model_change_discards_previous_window(monkeypatch):
    from novacode_cli.context import ContextBreakdown, ContextManager
    from novacode_cli.ui.ui_elements import TokenTracker

    tracker = TokenTracker()
    tracker.model_name = "old-model"
    tracker.add(50000, 1000)
    tracker.set_breakdown(ContextBreakdown(total_tokens=50000, context_window_size=200000))
    monkeypatch.setattr(ContextManager, "window_size", lambda self: 1000000)
    tracker.set_model("new-model")
    assert tracker.get_breakdown().context_window_size == 1000000
    assert tracker.session_total_tokens == 51000
