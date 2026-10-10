"""Tests for Nova learning middleware — counter, review triggers, injection.

Covers:
- Counter persistence in durable store
- Counter increment and reset
- Review injection logic
- Tool usage recording
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novacode_cli.hermes.middleware import NovaLearningMiddleware


@pytest.fixture
def mock_store():
    """Create a mock durable store with aput/aget."""
    store = MagicMock()
    store.aput = AsyncMock()
    store.aget = AsyncMock(return_value=None)  # Default: no existing data
    return store


@pytest.fixture
def middleware(mock_store):
    return NovaLearningMiddleware(
        store=mock_store,
        review_threshold=3,  # Low threshold for testing
        enabled=True,
        # The count-based trigger these tests exercise is the "periodic" mode;
        # the default ("task_end") is covered in tests/test_review_task_end.py.
        review_mode="periodic",
    )


class TestCounter:
    """Tool call counter persistence and lifecycle."""

    async def test_initial_count_is_zero(self, middleware, mock_store):
        """Counter should be 0 when no data exists in the store."""
        mock_store.aget.return_value = None
        count = await middleware._get_tool_call_count()
        assert count == 0

    async def test_increment_increases_count(self, middleware, mock_store):
        """Incrementing should increase the counter by 1."""
        mock_store.aget.return_value = MagicMock(value={"count": 5})
        new_count = await middleware._increment_counter()
        assert new_count == 6

    async def test_increment_from_zero(self, middleware, mock_store):
        """Incrementing from zero should set count to 1."""
        mock_store.aget.return_value = None
        new_count = await middleware._increment_counter()
        assert new_count == 1

    async def test_reset_counter(self, middleware, mock_store):
        """Reset should set counter to 0 and clear injected flag."""
        # Use a callable side_effect to simulate a stateful store
        store_state: dict = {
            ("nova", "tool_counter"): {"counter": {"count": 10}},
            ("nova", "meta"): {},
        }

        async def stateful_aget(namespace, key):
            ns = store_state.get(namespace, {})
            entry = ns.get(key)
            if entry is None:
                return None
            return MagicMock(value=dict(entry))

        async def stateful_aput(namespace, key, value):
            store_state.setdefault(namespace, {})[key] = dict(value)

        mock_store.aget.side_effect = stateful_aget
        mock_store.aput.side_effect = stateful_aput

        assert await middleware._get_tool_call_count() == 10
        await middleware._reset_counter()
        count = await middleware._get_tool_call_count()
        assert count == 0
        # review_just_completed flag should be set in durable store
        assert await middleware._get_review_just_completed() is True

    async def test_counter_persists_in_store(self, middleware, mock_store):
        """Counter should be persisted via store.aput."""
        mock_store.aget.return_value = None
        await middleware._increment_counter()
        mock_store.aput.assert_awaited_with(("nova", "tool_counter"), "counter", {"count": 1})

    async def test_cap_at_max(self, middleware, mock_store):
        """Counter should be capped at _MAX_COUNTER."""
        from novacode_cli.hermes.tracker import _MAX_COUNTER

        mock_store.aget.return_value = MagicMock(value={"count": _MAX_COUNTER})
        # Set the persisted value
        await middleware._set_tool_call_count(_MAX_COUNTER + 1)
        # Read back (should be capped)
        mock_store.aget.return_value = MagicMock(value={"count": _MAX_COUNTER + 1})
        count = await middleware._get_tool_call_count()
        assert count <= _MAX_COUNTER


class TestReviewTriggers:
    """Review threshold detection and injection."""

    async def test_should_review_below_threshold(self, middleware, mock_store):
        """should_review should be False when count is below threshold."""
        mock_store.aget.return_value = MagicMock(value={"count": 2})  # threshold=3
        assert not await middleware._should_review()

    async def test_should_review_at_threshold(self, middleware, mock_store):
        """should_review should be True when count equals threshold."""
        mock_store.aget.return_value = MagicMock(value={"count": 3})  # threshold=3
        assert await middleware._should_review()

    async def test_should_review_above_threshold(self, middleware, mock_store):
        """should_review should be True when count exceeds threshold."""
        mock_store.aget.return_value = MagicMock(value={"count": 10})  # threshold=3
        assert await middleware._should_review()

    async def test_apply_review_content_persists(self, middleware, mock_store):
        """Applying review content should persist review + meta to the store."""
        await middleware._apply_review_content("Lessons: learned a lot")
        namespaces = [c[0][0] for c in mock_store.aput.await_args_list]
        assert ("nova", "reviews") in namespaces
        assert ("nova", "meta") in namespaces


class TestToolUsageRecording:
    """Tool usage history tracking."""

    async def test_record_tool_usage_success(self, middleware, mock_store):
        """Successful tool call should be recorded."""
        mock_store.aget.return_value = None  # No existing history

        await middleware._record_tool_usage("edit_file", True)
        # Should have called aput with the history entry
        calls = mock_store.aput.await_args_list
        history_calls = [c for c in calls if c[0][0] == ("nova", "tool_history")]
        assert len(history_calls) >= 1

    async def test_record_tool_usage_failure(self, middleware, mock_store):
        """Failed tool call should still be recorded."""
        mock_store.aget.return_value = None
        await middleware._record_tool_usage("execute", False)
        calls = mock_store.aput.await_args_list
        history_calls = [c for c in calls if c[0][0] == ("nova", "tool_history")]
        assert len(history_calls) >= 1


class TestMiddlewareHooks:
    """High-level middleware hook integration."""

    async def test_enabled_false_is_noop(self, middleware, mock_store):
        """When enabled=False, hooks should pass through without tracking."""
        disabled = NovaLearningMiddleware(store=mock_store, enabled=False)
        call_fn = AsyncMock(return_value="result")
        # (request, handler) order
        result = await disabled.awrap_tool_call(MagicMock(tool_name="test"), call_fn)
        assert result == "result"
        # Should not have called any tracking
        mock_store.aput.assert_not_awaited()

    async def test_awrap_tool_call_increments_counter(self, middleware, mock_store):
        """Tool call wrapper should increment counter."""
        mock_store.aget.return_value = None  # Start from 0
        response = MagicMock(success=True)
        call_fn = AsyncMock(return_value=response)
        request = MagicMock(tool_name="edit_file")

        await middleware.awrap_tool_call(request, call_fn)

        # Counter should have been persisted
        counter_calls = [
            c for c in mock_store.aput.await_args_list if c[0][0] == ("nova", "tool_counter")
        ]
        assert len(counter_calls) >= 1


class TestOutOfBandReview:
    """Review runs out-of-band and never replaces the agent's task turn."""

    async def test_awrap_returns_real_response_untouched(self, middleware, mock_store):
        """Below threshold: the agent's real turn is returned unchanged, no review."""
        mock_store.aget.return_value = MagicMock(value={"count": 0})
        real_response = MagicMock()
        handler = AsyncMock(return_value=real_response)
        request = MagicMock()
        request.model = MagicMock()
        request.model.ainvoke = AsyncMock()

        result = await middleware.awrap_model_call(request, handler)

        assert result is real_response
        handler.assert_awaited_once_with(request)
        # No out-of-band review call below threshold
        request.model.ainvoke.assert_not_awaited()

    async def test_review_at_threshold_runs_out_of_band(self, middleware, mock_store):
        """At threshold: a SEPARATE model.ainvoke runs; the real turn is still returned."""
        import asyncio

        mock_store.aget.return_value = MagicMock(value={"count": 3})  # threshold=3
        real_response = MagicMock()
        handler = AsyncMock(return_value=real_response)

        review_ai = MagicMock()
        review_ai.content = '<lesson topic="testing">- learned X</lesson>'
        request = MagicMock()
        request.messages = []
        request.model = MagicMock()
        request.model.ainvoke = AsyncMock(return_value=review_ai)

        result = await middleware.awrap_model_call(request, handler)

        # The agent's real turn is returned UNTOUCHED (not the review response),
        # immediately — the review runs in the background and must not block it.
        assert result is real_response

        # Drain the background review task(s) scheduled by awrap_model_call.
        if middleware._refinement_tasks:
            await asyncio.gather(*list(middleware._refinement_tasks))

        # The review was issued as a separate, out-of-band model call.
        request.model.ainvoke.assert_awaited_once()
        # Review learnings were persisted.
        namespaces = [c[0][0] for c in mock_store.aput.await_args_list]
        assert ("nova", "reviews") in namespaces

    async def test_disabled_skips_review_and_returns_response(self, mock_store):
        """When disabled, no review runs and the response passes straight through."""
        disabled = NovaLearningMiddleware(store=mock_store, enabled=False)
        real = MagicMock()
        handler = AsyncMock(return_value=real)
        request = MagicMock()
        request.model = MagicMock()
        request.model.ainvoke = AsyncMock()

        result = await disabled.awrap_model_call(request, handler)

        assert result is real
        request.model.ainvoke.assert_not_awaited()

    async def test_apply_review_content_returns_none_when_disabled(self, mock_store):
        """Disabled middleware does not persist review content."""
        disabled = NovaLearningMiddleware(store=mock_store, enabled=False)
        result = await disabled._apply_review_content("anything")
        assert result is None
        mock_store.aput.assert_not_awaited()


class TestSyncToolCallHook:
    """Sync wrap_tool_call hook."""

    def test_disabled_passes_through(self, mock_store):
        """When enabled=False, handler is called and result returned directly."""
        disabled = NovaLearningMiddleware(store=mock_store, enabled=False)
        handler = MagicMock(return_value="direct_result")
        result = disabled.wrap_tool_call(MagicMock(tool_name="test"), handler)
        assert result == "direct_result"
        handler.assert_called_once()
        # No tracking calls
        mock_store.aput.assert_not_called()

    def test_happy_path_passthrough(self, middleware, mock_store):
        """Sync hook is a pure pass-through; tracking happens in the async hook."""
        response = MagicMock(success=True)
        handler = MagicMock(return_value=response)
        request = MagicMock(tool_name="edit_file")

        result = middleware.wrap_tool_call(request, handler)

        assert result is response
        handler.assert_called_once_with(request)
        # Sync path performs no durable-store tracking (async-only design).
        assert not mock_store.aput.called

    def test_exception_propagates(self, middleware, mock_store):
        """Sync pass-through re-raises handler exceptions without tracking."""
        handler = MagicMock(side_effect=ValueError("tool failed"))
        request = MagicMock(tool_name="bad_tool")

        with pytest.raises(ValueError, match="tool failed"):
            middleware.wrap_tool_call(request, handler)

        handler.assert_called_once_with(request)


class TestSyncModelCallHook:
    """Sync wrap_model_call hook."""

    def test_disabled_passes_through(self, mock_store):
        """When enabled=False, handler is called and result returned directly."""
        disabled = NovaLearningMiddleware(store=mock_store, enabled=False)
        handler = MagicMock(return_value="direct_result")
        result = disabled.wrap_model_call(MagicMock(messages=[]), handler)
        assert result == "direct_result"
        handler.assert_called_once()

    def test_no_review_below_threshold(self, middleware, mock_store):
        """When count is below threshold, handler is called without injection."""
        mock_store.aget.return_value = MagicMock(value={"count": 0})
        response = MagicMock()
        handler = MagicMock(return_value=response)

        result = middleware.wrap_model_call(MagicMock(messages=[]), handler)

        assert result is response
        handler.assert_called_once()

    def test_sync_wrap_is_passthrough(self, middleware, mock_store):
        """Sync wrap_model_call is a pure pass-through — reviews are async-only."""
        response = MagicMock()
        handler = MagicMock(return_value=response)
        request = MagicMock(messages=[])

        result = middleware.wrap_model_call(request, handler)

        assert result is response
        handler.assert_called_once_with(request)


class TestSessionConsolidation:
    """Session-end consolidation — distil un-reviewed work on quit/clear."""

    def _stateful_store(self, state: dict):
        """A store whose aget/aput read/write a plain dict (namespace→key→value)."""
        store = MagicMock()

        async def aget(namespace, key):
            entry = state.get(namespace, {}).get(key)
            if entry is None:
                return None
            return MagicMock(value=dict(entry))

        async def aput(namespace, key, value):
            state.setdefault(namespace, {})[key] = dict(value)

        store.aget.side_effect = aget
        store.aput.side_effect = aput
        return store

    def _request(self):
        """A ModelRequest whose model returns a review lesson."""
        review_ai = MagicMock()
        review_ai.content = '<lesson topic="testing">- learned X</lesson>'
        request = MagicMock()
        request.messages = []
        request.model = MagicMock()
        request.model.ainvoke = AsyncMock(return_value=review_ai)
        return request

    async def test_skips_without_live_request(self, mock_store):
        """No turn happened this session → nothing to consolidate, no model call."""
        mw = NovaLearningMiddleware(store=mock_store, enabled=True)
        assert await mw.consolidate_session() is False
        mock_store.aput.assert_not_awaited()

    async def test_skips_when_only_trivial_work(self):
        """Read-only browsing is not worth distilling into memory."""
        state = {
            ("nova", "tool_counter"): {"counter": {"count": 4}},
            ("nova", "tool_history"): {
                "history": {
                    "entries": [
                        {"tool": "read_file", "success": True, "timestamp": 1.0},
                        {"tool": "grep", "success": True, "timestamp": 2.0},
                        {"tool": "think", "success": True, "timestamp": 3.0},
                        {"tool": "web_search", "success": True, "timestamp": 4.0},
                    ]
                }
            },
            ("nova", "meta"): {"review_just_completed": {"value": False}},
        }
        store = self._stateful_store(state)
        mw = NovaLearningMiddleware(store=store, enabled=True)
        mw._last_request = self._request()

        assert await mw.consolidate_session() is False
        # No review model call was issued.
        mw._last_request.model.ainvoke.assert_not_awaited()

    async def test_skips_after_just_completed_review(self):
        """A review that just ran means there is nothing left to distill."""
        state = {
            ("nova", "tool_counter"): {"counter": {"count": 2}},
            ("nova", "tool_history"): {
                "history": {
                    "entries": [
                        {"tool": "edit_file", "success": True, "timestamp": 1.0},
                        {"tool": "execute", "success": True, "timestamp": 2.0},
                    ]
                }
            },
            ("nova", "meta"): {"review_just_completed": {"value": True}},
        }
        store = self._stateful_store(state)
        mw = NovaLearningMiddleware(store=store, enabled=True)
        mw._last_request = self._request()

        assert await mw.consolidate_session() is False
        mw._last_request.model.ainvoke.assert_not_awaited()

    async def test_runs_review_on_substantive_work(self):
        """Real edits below the threshold get distilled on the way out."""
        state = {
            ("nova", "tool_counter"): {"counter": {"count": 2}},
            ("nova", "tool_history"): {
                "history": {
                    "entries": [
                        {"tool": "edit_file", "success": True, "timestamp": 1.0},
                        {"tool": "execute", "success": True, "timestamp": 2.0},
                    ]
                }
            },
            ("nova", "meta"): {"review_just_completed": {"value": False}},
        }
        store = self._stateful_store(state)
        mw = NovaLearningMiddleware(store=store, enabled=True)
        mw._last_request = self._request()

        assert await mw.consolidate_session() is True
        # The review ran as an out-of-band model call.
        mw._last_request.model.ainvoke.assert_awaited_once()
        # The counter was reset so the next session starts clean.
        assert state[("nova", "tool_counter")]["counter"]["count"] == 0

    async def test_disabled_is_noop(self, mock_store):
        """Disabled middleware never consolidates."""
        mw = NovaLearningMiddleware(store=mock_store, enabled=False)
        mw._last_request = self._request()
        assert await mw.consolidate_session() is False
        mock_store.aput.assert_not_awaited()
