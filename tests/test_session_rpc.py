"""Exercise production worker RPC lifecycle without network/model calls."""

import asyncio

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.sessions.gateway import endpoint, request, session_events
from tests.test_session_worker import _make, _wait_for


async def test_worker_rpc_binds_turn_and_matches_only_its_reply(monkeypatch):
    received = []

    async def stream(*args, **kwargs):
        received.append(await request("list", {}))
        yield ev.Done()

    worker, frames = _make(monkeypatch, stream)
    worker._save = _noop
    run = asyncio.create_task(worker.run())
    await worker.inbox.put({"t": "prompt", "id": "peer-m1", "text": "hello"})
    assert await _wait_for(lambda: bool(worker._session_pending))
    rpc = next(frame for frame in frames if frame["t"] == "session_request")
    assert rpc["method"] == "list" and "sender" not in rpc
    await worker.inbox.put({"t": "session_reply", "id": "wrong", "result": {"forged": True}})
    await asyncio.sleep(0.03)
    assert not received
    await worker.inbox.put({"t": "session_reply", "id": rpc["id"], "result": {"sessions": ["b"]}})
    assert await _wait_for(lambda: bool(received))
    assert received == [{"sessions": ["b"]}]
    assert await _wait_for(lambda: not worker._session_pending)
    assert endpoint.get() is None
    await worker.inbox.put({"t": "shutdown"})
    assert await asyncio.wait_for(run, 3) == 0


async def _noop():
    pass


async def test_worker_rpc_limits_cancellation_and_shutdown(monkeypatch):
    worker, frames = _make(monkeypatch, [])
    worker._save = _noop
    requests = [asyncio.create_task(worker.session_request("read", {})) for _ in range(16)]
    await asyncio.sleep(0)
    assert "Too many" in (await worker.session_request("read", {}))["error"]
    requests[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await requests[0]
    assert len(worker._session_pending) == 15
    await worker.inbox.put({"t": "shutdown"})
    assert await worker.run() == 0
    results = await asyncio.gather(*requests[1:])
    assert all("closed" in result["error"] for result in results)
    assert not worker._session_pending
    assert "closed" in (await worker.session_request("list", {}))["error"]


async def test_rpc_timeout_releases_waiter(monkeypatch):
    worker, frames = _make(monkeypatch, [])
    original = asyncio.wait_for

    async def quick(future, timeout):
        assert timeout == 10
        return await original(future, 0.01)

    monkeypatch.setattr(asyncio, "wait_for", quick)
    assert "timeout" in (await worker.session_request("read", {}))["error"]
    assert not worker._session_pending


async def test_stream_closes_source_and_restores_context():
    closed = []

    async def source():
        try:
            yield await request("read", {})
            yield "never"
        finally:
            closed.append(True)

    async def route(method, payload):
        return {"owner": "root"}

    events = session_events(source(), route)
    assert await anext(events) == {"owner": "root"}
    await events.aclose()
    assert closed == [True] and endpoint.get() is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_worker_failure_events_keep_prompt_correlation(monkeypatch, cancel):
    entered = asyncio.Event()

    async def stream(*args, **kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("fixture failure")
        yield ev.Done()

    worker, frames = _make(monkeypatch, stream)
    task = asyncio.create_task(worker._run_turn("peer-m1", "Q"))
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    events = [frame for frame in frames if frame["t"] == "ev"]
    assert events and all(frame.get("prompt_id") == "peer-m1" for frame in events)
