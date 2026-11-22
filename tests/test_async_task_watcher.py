"""AsyncTaskWatcher: watch only running async-subagent tasks, dedup, and notify
with the right level when one finishes (closing the fire-and-forget gap)."""

from __future__ import annotations

import asyncio

from novacode_cli.remote.async_task_watcher import AsyncTaskWatcher


async def test_watches_only_running_and_dedups():
    scheduled: list[str] = []

    w = AsyncTaskWatcher(notify=lambda *a: None)

    async def fake_poll(task):
        scheduled.append(task["task_id"])

    w._poll = fake_poll  # avoid real SDK/network

    tasks = {
        "a": {"task_id": "a", "status": "running", "agent_name": "x", "thread_id": "a", "run_id": "r"},
        "b": {"task_id": "b", "status": "success", "agent_name": "x"},  # not running → skip
    }
    w.sync_from_state(tasks)
    w.sync_from_state(tasks)  # same tasks again → 'a' must not be re-watched
    await asyncio.sleep(0.02)

    assert scheduled == ["a"]  # only the running one, exactly once


async def test_ignores_empty_and_malformed():
    w = AsyncTaskWatcher(notify=lambda *a: None)
    w._poll = lambda task: asyncio.sleep(0)  # never records
    w.sync_from_state(None)
    w.sync_from_state({"x": "not-a-dict"})
    await asyncio.sleep(0.01)
    assert set(w._watched) == set()


async def test_running_tasks_lists_watched_with_runtime():
    w = AsyncTaskWatcher(notify=lambda *a: None)

    async def slow(task):  # keep it "running"
        await asyncio.sleep(5)

    w._poll = slow
    w.sync_from_state(
        {"a": {"task_id": "a", "status": "running", "agent_name": "code-review-agent",
               "thread_id": "a", "run_id": "r"}}
    )
    rt = w.running_tasks()
    assert len(rt) == 1
    assert rt[0]["agent_name"] == "code-review-agent"
    assert rt[0]["runtime"] >= 0
    for t in list(w._tasks):
        t.cancel()


async def test_poll_removes_from_running_on_exit():
    w = AsyncTaskWatcher(notify=lambda *a: None)
    w._running["x"] = {"agent_name": "unknown-agent", "started": 0.0}
    # Unknown agent → no spec → _poll returns immediately; its finally pops _running.
    await w._poll({"task_id": "x", "agent_name": "unknown-agent", "thread_id": "x", "run_id": "r"})
    assert "x" not in w._running
    assert w.running_tasks() == []


def test_emit_maps_status_to_level_and_message():
    got: list[tuple] = []
    w = AsyncTaskWatcher(notify=lambda level, title, msg: got.append((level, title, msg)))

    w._emit("code-review-agent", "abcdef123456", "success")
    w._emit("documentation-update-agent", "zzzzzzzz1111", "error")

    assert got[0][0] == "success"
    assert "code-review-agent" in got[0][2] and "abcdef12" in got[0][2]

    assert got[1][0] == "warning"
    assert "error" in got[1][1].lower()


async def test_slow_task_keeps_monitoring_and_delivers_result(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from novacode_cli.remote import async_task_watcher as module

    notices = []
    client = SimpleNamespace(
        runs=SimpleNamespace(get=AsyncMock(side_effect=[
            {"status": "running"}, {"status": "running"}, {"status": "success"},
        ])), aclose=AsyncMock(),
    )
    monkeypatch.setattr("langgraph_sdk.get_client", lambda **kwargs: client)
    monkeypatch.setattr(module, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(module, "_MAX_WATCH_SECONDS", 0)
    watcher = AsyncTaskWatcher(lambda *args: notices.append(args))
    watcher._spec_for = lambda name: {"url": "http://test.invalid"}
    watcher.sync_from_state({"a": {"status": "running", "agent_name": "test", "run_id": "r", "thread_id": "t"}})
    await asyncio.gather(*watcher._tasks)
    assert len(notices) == 2
    assert "Taking longer" in notices[0][1]
    assert "async_task_id=" not in notices[0][2]
    assert notices[1][0] == "success"
    assert client.runs.get.await_count == 3
    assert not watcher.running_tasks()
    assert "a" in watcher._watched
    client.aclose.assert_not_awaited()  # Shared until this session's watcher closes.
    await watcher.aclose()
    client.aclose.assert_awaited_once()


async def test_connection_failure_is_visible_and_recovers(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from novacode_cli.remote import async_task_watcher as module

    notices = []
    client = SimpleNamespace(
        runs=SimpleNamespace(get=AsyncMock(side_effect=[
            OSError("offline"), OSError("offline"), OSError("offline"),
            OSError("offline"), {"status": "success"},
        ])), aclose=AsyncMock(),
    )
    monkeypatch.setattr("langgraph_sdk.get_client", lambda **kwargs: client)
    monkeypatch.setattr(module, "_POLL_INTERVAL", 0)
    watcher = AsyncTaskWatcher(lambda *args: notices.append(args))
    watcher._spec_for = lambda name: {"url": "http://test.invalid"}
    await watcher._poll({"task_id": "a", "thread_id": "t", "run_id": "r"})
    assert len(notices) == 3
    assert "Connection interrupted" in notices[0][1]
    assert "Connection restored" in notices[1][1]
    assert notices[2][0] == "success"
    await watcher.aclose()


async def test_failed_setup_can_be_watched_again():
    watcher = AsyncTaskWatcher(lambda *args: None)
    watcher._spec_for = lambda name: None
    state = {"a": {"status": "running", "agent_name": "missing", "thread_id": "t", "run_id": "r"}}
    watcher.sync_from_state(state)
    await asyncio.gather(*watcher._tasks)
    assert "a" not in watcher._watched
    watcher.sync_from_state(state)
    await asyncio.gather(*watcher._tasks)
    await watcher.aclose()


async def test_setup_failures_stop_requeued_polling_after_a_budget():
    """Transient setup failures retry, then the task is declared unmonitorable.

    Regression: a record whose agent spec no longer resolved was popped and
    re-enqueued by every turn-end sync forever — silent infinite retry.
    """
    notices: list[tuple[str, str]] = []
    watcher = AsyncTaskWatcher(lambda level, title, message: notices.append((title, message)))
    watcher._spec_for = lambda name: None
    state = {"a": {"status": "running", "agent_name": "missing", "thread_id": "t", "run_id": "r"}}
    from novacode_cli.remote import async_task_watcher as module

    for _ in range(module._MAX_SETUP_TRIES):
        watcher.sync_from_state(state)
        await asyncio.gather(*watcher._tasks)
    watcher.sync_from_state(state)
    await asyncio.gather(*watcher._tasks)
    await watcher.aclose()
    assert "a" in watcher._watched, "must stop re-enqueueing an unmonitorable record"
    assert any("monitorable" in title.lower() for title, _ in notices)


async def test_a_record_without_ids_is_never_watched_again():
    notices: list[tuple[str, str]] = []
    watcher = AsyncTaskWatcher(lambda level, title, message: notices.append((title, message)))
    state = {"a": {"status": "running", "agent_name": "x"}}
    watcher.sync_from_state(state)
    await asyncio.gather(*watcher._tasks)
    watcher.sync_from_state(state)
    await asyncio.gather(*watcher._tasks)
    await watcher.aclose()
    assert "a" in watcher._watched
    assert any("monitorable" in title.lower() for title, _ in notices)


async def test_eviction_spares_a_poll_in_flight(monkeypatch: pytest.MonkeyPatch):
    """Evicting an actively-polled id lets a later sync re-watch it and emit a
    second completion (second auto-report turn); eviction skips those ids."""
    from novacode_cli.remote import async_task_watcher as module

    monkeypatch.setattr(module, "_MAX_WATCHED_IDS", 4)
    monkeypatch.setattr(module, "_MAX_PENDING_POLLS", 64)
    watcher = module.AsyncTaskWatcher(lambda *args: None)
    stuck = asyncio.Event()

    class _Runs:
        @staticmethod
        async def get(**_kw):
            await stuck.wait()
            return {"status": "success"}

    class _Client:
        runs = _Runs()

    async def fake_client(spec, factory):
        return _Client()

    watcher._clients.get = fake_client
    ids = [f"t{i}" for i in range(10)]
    state = {
        i: {"status": "running", "agent_name": "x", "thread_id": i, "run_id": i}
        for i in ids
    }
    for i in ids:
        watcher._running[i] = {"agent_name": "x", "started": 0.0}
    watcher.sync_from_state(state)
    # All 10 polls are in flight (in _running), so all 10 survive the cap of 4.
    assert set(watcher._watched) == set(ids)
    stuck.set()
    await watcher.aclose()


async def test_invalidate_specs_forgets_the_spec_cache(monkeypatch: pytest.MonkeyPatch):
    """A /model change to the async role must not leave stale poll specs."""
    from novacode_cli.agents.default_subagents import async_subagents
    from novacode_cli.remote import async_task_watcher as module

    monkeypatch.setattr(
        async_subagents,
        "retrieve_async_subagents",
        lambda: [{"name": "x", "url": "old"}],
    )
    watcher = module.AsyncTaskWatcher(lambda *args: None)
    assert await watcher._resolve_spec("x") == {"name": "x", "url": "old"}
    assert watcher._specs is not None  # cache populated
    await watcher.invalidate_specs()
    assert watcher._specs is None
    # Re-discovery happens after invalidation, and setup retries are reset.
    watcher._setup_tries["a"] = 2
    await watcher.invalidate_specs()
    assert not watcher._setup_tries


async def test_invalidate_specs_cannot_race_a_cold_discovery(monkeypatch: pytest.MonkeyPatch):
    """The clear holds _spec_lock, so a discovery in flight finishes before
    invalidation — it cannot write its stale specs back after the clear."""
    import asyncio
    import threading

    from novacode_cli.agents.default_subagents import async_subagents
    from novacode_cli.remote import async_task_watcher as module

    monkeypatch.setattr(
        async_subagents,
        "retrieve_async_subagents",
        lambda: [{"name": "x", "url": "old"}],
    )
    watcher = module.AsyncTaskWatcher(lambda *args: None)

    discovery_entered = threading.Event()
    release = threading.Event()
    real_spec_for = module.AsyncTaskWatcher._spec_for

    def slow_spec_for(self, agent_name):
        discovery_entered.set()  # discovery is running, holding the lock
        release.wait(5)
        return real_spec_for(self, agent_name)

    monkeypatch.setattr(module.AsyncTaskWatcher, "_spec_for", slow_spec_for)

    discovery = asyncio.create_task(watcher._resolve_spec("x"))
    # Pump the loop until the discovery thread has entered _spec_for.
    for _ in range(500):
        if discovery_entered.is_set():
            break
        await asyncio.sleep(0.01)
    else:
        discovery.cancel()
        raise AssertionError("cold discovery never entered _spec_for")
    invalidate = asyncio.create_task(watcher.invalidate_specs())
    await asyncio.sleep(0)  # let invalidate_specs block on the held lock
    assert invalidate.done() is False, "invalidation waits for the discovery lock"
    release.set()
    assert await discovery == {"name": "x", "url": "old"}
    await invalidate
    assert watcher._specs is None, "stale discovery result was cleared"


async def test_slow_tasks_do_not_starve_later_completions(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from novacode_cli.remote import async_task_watcher as module

    monkeypatch.setattr(module, "_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(module, "_MAX_CONCURRENT_POLLS", 2)
    finished = asyncio.Event()
    active = 0
    maximum = 0

    async def get_run(thread_id, run_id):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.005)
            return {"status": "success" if run_id == "last" else "running"}
        finally:
            active -= 1

    def notify(level, title, message):
        if level == "success":
            finished.set()

    watcher = AsyncTaskWatcher(notify)
    watcher._spec_for = lambda name: {"url": "http://test.invalid"}
    monkeypatch.setattr("langgraph_sdk.get_client", lambda **kwargs: SimpleNamespace(
        runs=SimpleNamespace(get=get_run), aclose=AsyncMock(),
    ))
    watcher.sync_from_state({str(i): {
        "status": "running", "thread_id": str(i), "run_id": "last" if i == 4 else str(i),
    } for i in range(5)})
    try:
        await asyncio.wait_for(finished.wait(), 2)
        assert maximum <= 2
        assert len(watcher.running_tasks()) == 4
    finally:
        await asyncio.wait_for(watcher.aclose(), 2)
    assert not watcher._tasks
