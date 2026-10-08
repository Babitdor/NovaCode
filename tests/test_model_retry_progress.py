"""Only retry model requests, report every retry, and stop on permanent errors."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


def middleware(monkeypatch):
    from novacode_cli.agents import model_retry

    events = []
    monkeypatch.setattr(model_retry, "get_stream_writer", lambda: events.append)
    return model_retry.NovaModelRetryMiddleware(initial_delay=0, jitter=False), events


async def test_three_retries_then_final_failure(monkeypatch):
    retry, events = middleware(monkeypatch)
    failure = RuntimeError("Error code: 500 - Upstream request failed: Endpoint is unavailable.")
    handler = AsyncMock(side_effect=failure)
    with pytest.raises(RuntimeError, match="Endpoint is unavailable"):
        await retry.awrap_model_call(Mock(), handler)
    assert handler.await_count == 4
    assert [event["attempt"] for event in events if event["phase"] == "retry"] == [1, 2, 3]
    from novacode_cli.errors.provider_errors import friendly_model_error

    assert "temporarily unavailable" in friendly_model_error(failure)


async def test_retry_success_restores_activity(monkeypatch):
    retry, events = middleware(monkeypatch)
    result = Mock()
    handler = AsyncMock(side_effect=[TimeoutError("timeout"), result])
    assert await retry.awrap_model_call(Mock(), handler) is result
    assert [event["phase"] for event in events] == ["retry", "recovered"]


@pytest.mark.parametrize(
    "message", ["invalid API key", "quota exceeded", "context length exceeded"]
)
async def test_permanent_and_context_errors_are_not_retried(monkeypatch, message):
    retry, events = middleware(monkeypatch)
    handler = AsyncMock(side_effect=RuntimeError(message))
    with pytest.raises(RuntimeError):
        await retry.awrap_model_call(Mock(), handler)
    assert handler.await_count == 1 and not events


async def test_cancellation_during_retry_wait_stops_requests(monkeypatch):
    retry, events = middleware(monkeypatch)
    retry.initial_delay = 30
    handler = AsyncMock(side_effect=TimeoutError("timeout"))
    task = asyncio.create_task(retry.awrap_model_call(Mock(), handler))
    await asyncio.sleep(0)
    assert events[0]["phase"] == "retry"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handler.await_count == 1


def test_sync_retries_report_progress(monkeypatch):
    retry, events = middleware(monkeypatch)
    result = Mock()
    handler = Mock(side_effect=[ConnectionError("connection refused"), result])
    assert retry.wrap_model_call(Mock(), handler) is result
    assert [event["phase"] for event in events] == ["retry", "recovered"]


def test_retry_custom_events_reach_live_status():
    from novacode_cli import ui_events as ev
    from tests.test_agent_stream import _collect, _State

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            for attempt in range(1, 4):
                yield ((), "custom", {
                    "type": "nova_model_retry", "phase": "retry", "attempt": attempt, "maximum": 3,
                })
            yield ((), "custom", {"type": "nova_model_retry", "phase": "recovered"})

        async def aupdate_state(self, **kwargs):
            pass

    messages = [event.message for event in _collect(Agent()) if isinstance(event, ev.StatusUpdate)]
    assert messages[1:4] == [f"Retrying model request · {attempt}/3…" for attempt in range(1, 4)]
    assert messages[4] == "thinking…"


async def test_real_graph_stream_publishes_retry_progress_before_recovery():
    from langgraph.graph import END, START, StateGraph

    from novacode_cli.agents.model_retry import NovaModelRetryMiddleware

    retry = NovaModelRetryMiddleware(initial_delay=0, jitter=False)
    handler = AsyncMock(side_effect=[TimeoutError("timeout"), Mock()])

    async def call_model(state):
        await retry.awrap_model_call(Mock(), handler)
        return state

    graph = StateGraph(dict)
    graph.add_node("model", call_model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    events = [event async for event in graph.compile().astream({}, stream_mode="custom")]
    assert [event["phase"] for event in events] == ["retry", "recovered"]
    assert events[0]["attempt"] == 1 and events[0]["maximum"] == 3
    assert handler.await_count == 2
