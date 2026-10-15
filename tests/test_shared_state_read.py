"""The turn's end reads agent state once, not three times (each read can freeze the UI)."""

import asyncio

import pytest


def _app():
    from tests.test_tui_compaction_row import _app as build

    return build()


class _Agent:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.calls, self.delay, self.fail = 0, delay, fail

    async def aget_state(self, config):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("database busy")
        return {"n": self.calls}


@pytest.mark.asyncio
async def test_reads_at_a_turn_end_share_one_state_read():
    app, agent = _app(), _Agent(delay=0.05)
    config = {"configurable": {"thread_id": "t"}}
    app._share_turn_end_reads(True)  # what Done does
    results = await asyncio.gather(*(app._read_state(agent, config) for _ in range(3)))
    assert agent.calls == 1 and results == [{"n": 1}] * 3
    await app._read_state(agent, {"configurable": {"thread_id": "other"}})
    assert agent.calls == 2, "another conversation is read on its own"


@pytest.mark.asyncio
async def test_outside_a_turn_end_every_read_is_fresh():
    """A /compact or /clear right after a turn must see the state it changed."""
    app, agent = _app(), _Agent()
    config = {"configurable": {"thread_id": "t"}}
    app._share_turn_end_reads(True)
    await app._read_state(agent, config)
    app._share_turn_end_reads(False)  # the wrap-up is over
    assert await app._read_state(agent, config) == {"n": 2}
    app._share_turn_end_reads(True)  # the next turn's Done drops the old read
    assert await app._read_state(agent, config) == {"n": 3}


@pytest.mark.asyncio
async def test_a_failed_or_timed_out_read_is_not_shared():
    app = _app()
    config = {"configurable": {"thread_id": "t"}}
    app._share_turn_end_reads(True)
    broken = _Agent(fail=True)
    with pytest.raises(RuntimeError):
        await app._read_state(broken, config)
    with pytest.raises(RuntimeError):
        await app._read_state(broken, config)
    assert broken.calls == 2

    slow = _Agent(delay=0.2)
    with pytest.raises(asyncio.TimeoutError):
        await app._read_state(slow, config, timeout=0.01)
    assert await app._read_state(slow, config) == {"n": 1}, "the shared read was not cancelled"
    assert slow.calls == 1
