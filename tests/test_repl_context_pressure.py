"""The Rich REPL must react to context pressure, not just record it.

The TUI warned and auto-compacted; ``ui/execution.py`` recorded the breakdown
and did nothing, so a long console session ran straight into the provider's
limit. The shared policy (``context/pressure.py``) now drives both.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.context import AUTO_COMPACT_THRESHOLD
from novacode_cli.ui.execution import _react_to_context_pressure


class _Breakdown:
    def __init__(self, pct: float) -> None:
        self.usage_percentage = pct


class _State:
    def __init__(self) -> None:
        self._auto_compact = True
        self._compacted_last_turn = False
        self.session_id = "s1"
        self.agent_dir = None
        self.model: object | None = None


class _Tracker:
    model_name = "gpt-4o"

    def __init__(self) -> None:
        self.reset_calls = 0

    def reset(self, **_kw) -> None:
        self.reset_calls += 1

    def get_breakdown(self) -> _Breakdown:
        return _Breakdown(10.0)


@pytest.mark.asyncio
async def test_low_usage_is_a_no_op() -> None:
    state = _State()
    await _react_to_context_pressure(object(), state, _Tracker(), _Breakdown(5.0))
    assert state._compacted_last_turn is False


@pytest.mark.asyncio
async def test_critical_usage_without_a_model_degrades_to_a_warning() -> None:
    """No reachable BaseChatModel -> warn and point at /compact, never crash."""
    state = _State()
    pct = AUTO_COMPACT_THRESHOLD * 100 + 1

    await _react_to_context_pressure(object(), state, _Tracker(), _Breakdown(pct))

    # It must not have claimed to compact, and must not have thrown.
    assert state._compacted_last_turn is False


@pytest.mark.asyncio
async def test_repeated_pressure_disables_auto_compact_via_shared_policy() -> None:
    """Second critical turn in a row trips the loop guard for the REPL too."""
    state = _State()
    state._compacted_last_turn = True
    pct = AUTO_COMPACT_THRESHOLD * 100 + 1

    await _react_to_context_pressure(object(), state, _Tracker(), _Breakdown(pct))

    assert state._auto_compact is False


@pytest.mark.asyncio
async def test_never_raises_on_a_broken_state_object() -> None:
    """A notice must never break a completed turn."""
    await _react_to_context_pressure(
        object(), SimpleNamespace(), _Tracker(), _Breakdown(99.0)
    )


# -- the compaction path (a reachable model) -------------------------------


def _fake_model() -> Any:
    """A real (fake) chat model, so the ``isinstance`` gate is satisfied."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    return FakeListChatModel(responses=["ok"])


class _RealishTracker(_Tracker):
    def __init__(self) -> None:
        super().__init__()
        self.breakdowns = [_Breakdown(20.0)]

    def get_breakdown(self) -> _Breakdown:
        return self.breakdowns[0]


@pytest.mark.asyncio
async def test_compaction_runs_when_a_model_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual gap: the REPL now compacts instead of sailing past the limit."""
    from novacode_cli import compaction
    from novacode_cli.context import CompactionResult

    calls: dict[str, object] = {}

    async def _fake_compact(agent, model, thread_id, **kw):  # noqa: ANN001, ANN202, ARG001
        calls["model"] = model
        calls["thread_id"] = thread_id
        return CompactionResult(
            success=True,
            original_tokens=100,
            new_tokens=10,
            tokens_saved=90,
            messages_before=5,
            messages_after=1,
            summary="s",
        )

    monkeypatch.setattr(compaction, "compact_conversation", _fake_compact)

    state = _State()
    state.model = _fake_model()
    tracker = _RealishTracker()
    pct = AUTO_COMPACT_THRESHOLD * 100 + 1

    await _react_to_context_pressure(object(), state, tracker, _Breakdown(pct))

    assert calls.get("thread_id") == "s1", "compaction did not run"
    assert tracker.reset_calls == 1, "the tracker was not reset after compacting"
    assert state._compacted_last_turn is True


@pytest.mark.asyncio
async def test_failed_compaction_does_not_reset_the_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed compaction must not look like freed space."""
    from novacode_cli import compaction
    from novacode_cli.context import CompactionResult

    async def _fail(agent, model, thread_id, **kw):  # noqa: ANN001, ANN202, ARG001
        return CompactionResult(
            success=False,
            original_tokens=0,
            new_tokens=0,
            tokens_saved=0,
            messages_before=0,
            messages_after=0,
            summary="",
            error="provider down",
        )

    monkeypatch.setattr(compaction, "compact_conversation", _fail)

    state = _State()
    state.model = _fake_model()
    tracker = _RealishTracker()

    await _react_to_context_pressure(
        object(), state, tracker, _Breakdown(AUTO_COMPACT_THRESHOLD * 100 + 1)
    )

    assert tracker.reset_calls == 0
    assert state._compacted_last_turn is False


@pytest.mark.asyncio
async def test_still_critical_after_compaction_disables_auto_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A too-small window: compaction can't win, so stop trying every turn."""
    from novacode_cli import compaction
    from novacode_cli.context import CompactionResult

    async def _ok(agent, model, thread_id, **kw):  # noqa: ANN001, ANN202, ARG001
        return CompactionResult(
            success=True,
            original_tokens=100,
            new_tokens=90,
            tokens_saved=10,
            messages_before=5,
            messages_after=1,
            summary="s",
        )

    monkeypatch.setattr(compaction, "compact_conversation", _ok)

    state = _State()
    state.model = _fake_model()
    tracker = _RealishTracker()
    # Post-compaction usage is STILL over the threshold.
    tracker.breakdowns = [_Breakdown(AUTO_COMPACT_THRESHOLD * 100 + 5)]

    await _react_to_context_pressure(
        object(), state, tracker, _Breakdown(AUTO_COMPACT_THRESHOLD * 100 + 1)
    )

    assert state._auto_compact is False

