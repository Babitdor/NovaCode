"""A mid-turn library compaction must emit CompactionNotice.

Two compaction shapes exist and only one shrinks the message list:

* Nova's ``/compact`` REPLACES history with a summary — the count drops.
* deepagents' ``SummarizationMiddleware`` does NOT delete messages; it records a
  ``_summarization_event`` holding a cutoff index and rebuilds the effective
  list from it. The count is unchanged.

Detecting only the count drop meant a library compaction emitted nothing, so
the token tracker kept the turn's PRE-compaction API total (its peak) and
reported ~85% when the real context was ~10% — and Nova then auto-compacted an
already-compacted conversation. That is the double-compaction the user saw.
"""

from __future__ import annotations

import asyncio

import novacode_cli.ui_events as ev
from novacode_cli.core.agent_loop import iterate_agent_events


class _Chunk:
    def __init__(self, mid, blocks):
        self.id = mid
        self._blocks = blocks
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 2}

    @property
    def content_blocks(self):
        return self._blocks


class _State:
    def __init__(self, values):
        self.values = values


class _SessionState:
    thread_id = "t-compaction"


def _agent(pre_values, post_values):
    """Agent whose state differs before and after the stream."""

    class Agent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            self.calls += 1
            return _State(pre_values if self.calls == 1 else post_values)

        async def astream(self, inp, **kw):
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "ok"}]), {}))

        async def aupdate_state(self, **kw):
            pass

    return Agent()


def _run(agent):
    async def _go():
        return [
            e
            async for e in iterate_agent_events("hi", agent, "nova-agent", _SessionState())
        ]

    return asyncio.run(_go())


def _msgs(n):
    return [object()] * n


def test_library_compaction_is_detected_without_a_count_drop():
    """The reported bug: same message count, but the library compacted."""
    events = _run(
        _agent(
            {"messages": _msgs(40)},
            {"messages": _msgs(40), "_summarization_event": {"cutoff_index": 30}},
        )
    )
    assert any(isinstance(e, ev.CompactionNotice) for e in events), (
        "a library compaction emitted no CompactionNotice, so the stale "
        "pre-compaction token total would trigger a second compaction"
    )


def test_a_preexisting_summarization_event_is_not_reported_again():
    """Only a NEW event is news — otherwise every later turn re-fires."""
    event = {"cutoff_index": 30}
    events = _run(
        _agent(
            {"messages": _msgs(40), "_summarization_event": event},
            {"messages": _msgs(41), "_summarization_event": event},
        )
    )
    assert not any(isinstance(e, ev.CompactionNotice) for e in events)


def test_nova_compaction_still_detected_by_the_count_drop():
    """The original path must keep working: /compact collapses history."""
    events = _run(_agent({"messages": _msgs(40)}, {"messages": _msgs(1)}))
    assert any(isinstance(e, ev.CompactionNotice) for e in events)


def test_an_ordinary_turn_emits_no_notice():
    """A turn that merely appends messages is not a compaction."""
    events = _run(_agent({"messages": _msgs(10)}, {"messages": _msgs(12)}))
    assert not any(isinstance(e, ev.CompactionNotice) for e in events)


def test_usage_precedes_compaction_reset():
    events = _run(_agent({"messages": _msgs(40)}, {"messages": _msgs(1)}))
    kinds = [type(e) for e in events]
    assert kinds.index(ev.UsageUpdate) < kinds.index(ev.CompactionNotice)


def test_a_later_summarization_event_is_detected():
    events = _run(_agent(
        {"messages": _msgs(40), "_summarization_event": {"cutoff_index": 10}},
        {"messages": _msgs(41), "_summarization_event": {"cutoff_index": 30}},
    ))
    assert any(isinstance(e, ev.CompactionNotice) for e in events)
