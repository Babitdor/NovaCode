"""Background observation leaves input responsive and releases HTTP resources."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote.async_task_clients import AsyncTaskClients
from novacode_cli.remote.async_task_watcher import AsyncTaskWatcher
from novacode_cli.tools import waiter_tools


async def test_watcher_checks_completion_before_first_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    client = SimpleNamespace(
        runs=SimpleNamespace(get=AsyncMock(return_value={"status": "success"})),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr("langgraph_sdk.get_client", lambda **_kwargs: client)
    completed = asyncio.Event()
    watcher = AsyncTaskWatcher(lambda *_args: completed.set())
    watcher._spec_for = lambda _name: {"url": "http://test.invalid"}
    watcher.sync_from_state(
        {
            "task": {
                "status": "running",
                "agent_name": "test",
                "thread_id": "thread",
                "run_id": "run",
            }
        }
    )
    try:
        await asyncio.wait_for(completed.wait(), 0.5)
    finally:
        await watcher.aclose()
    client.runs.get.assert_awaited_once()
    client.aclose.assert_awaited_once()


async def test_waiter_releases_client_after_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        lambda: [{"name": "test", "url": "http://test.invalid"}],
    )
    client = SimpleNamespace(
        runs=SimpleNamespace(get=AsyncMock(return_value={"status": "success"})),
        threads=SimpleNamespace(get=AsyncMock(return_value={"values": {"messages": []}})),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(waiter_tools, "_remote_client", lambda _spec: client)
    result = await waiter_tools._wait_remote(
        {
            "task_id": "task",
            "agent_name": "test",
            "thread_id": "thread",
            "run_id": "run",
        },
        1,
    )
    assert result["status"] == "success"
    client.aclose.assert_awaited_once()


async def test_waiter_discovery_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch):
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    discovery_finished = threading.Event()
    observed_while_blocked = []

    def discover() -> list:
        loop.call_soon_threadsafe(entered.set)
        release.wait(0.25)
        discovery_finished.set()
        return []

    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        discover,
    )

    async def input_pulse() -> None:
        await entered.wait()
        observed_while_blocked.append(not discovery_finished.is_set())
        release.set()

    pulse = asyncio.create_task(input_pulse())
    try:
        await waiter_tools._wait_remote({"task_id": "task", "agent_name": "test"}, 1)
        await pulse
        assert observed_while_blocked == [True]
    finally:
        release.set()
        await pulse


async def test_concurrent_monitoring_shares_setup_per_route(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import Mock

    from novacode_cli.remote import async_task_watcher as module

    client = SimpleNamespace(
        runs=SimpleNamespace(get=AsyncMock(return_value={"status": "success"})),
        aclose=AsyncMock(),
    )
    build = Mock(return_value=client)
    discover = Mock(return_value=[{"name": "test", "url": "http://test.invalid"}])
    monkeypatch.setattr("langgraph_sdk.get_client", build)
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        discover,
    )
    watcher = module.AsyncTaskWatcher(lambda *_args: None)
    watcher.sync_from_state(
        {
            str(index): {
                "status": "running",
                "agent_name": "test",
                "thread_id": str(index),
                "run_id": "run",
            }
            for index in range(64)
        }
    )
    try:
        await asyncio.wait_for(asyncio.gather(*tuple(watcher._tasks)), 2)
        assert build.call_count == 1
        assert discover.call_count == 1
        assert client.runs.get.await_count == 64
        client.aclose.assert_not_awaited()
    finally:
        await watcher.aclose()
    client.aclose.assert_awaited_once()


async def test_clients_preserve_route_and_authorization_isolation():
    from unittest.mock import Mock

    owners = [AsyncTaskClients(), AsyncTaskClients()]
    clients = [SimpleNamespace(aclose=AsyncMock()) for _ in range(4)]
    create = Mock(side_effect=clients)
    specs = [
        {"url": "http://one.invalid", "headers": {"authorization": "first"}},
        {"url": "http://two.invalid", "headers": {"authorization": "first"}},
        {"url": "http://one.invalid", "headers": {"authorization": "second"}},
    ]
    try:
        actual = [await owners[0].get(spec, create) for spec in specs]
        assert actual == clients[:3]
        assert await owners[0].get(specs[0], create) is clients[0]
        assert await owners[1].get(specs[0], create) is clients[3]
    finally:
        for owner in owners:
            await owner.aclose()
    for client in clients:
        client.aclose.assert_awaited_once()


async def test_cancelled_setup_closes_late_client_without_delaying_input():
    owner = AsyncTaskClients()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    client = SimpleNamespace(aclose=AsyncMock())

    def create() -> SimpleNamespace:
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        return client

    waiting = asyncio.create_task(owner.get({"url": "http://test.invalid"}, create))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(owner.aclose(), 0.5)
        client.aclose.assert_not_awaited()
        setup = tuple(owner._clients.values())
        release.set()
        await asyncio.wait_for(asyncio.gather(*setup), 1)
        client.aclose.assert_awaited_once()
        assert not owner._clients
    finally:
        release.set()
        await owner.aclose()


async def test_cancelling_one_shared_setup_does_not_cancel_other_observer():
    from unittest.mock import Mock

    owner = AsyncTaskClients()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    client = SimpleNamespace(aclose=AsyncMock())

    def create() -> SimpleNamespace:
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        return client

    build = Mock(side_effect=create)
    spec = {"url": "http://test.invalid"}
    first = asyncio.create_task(owner.get(spec, build))
    second = asyncio.create_task(owner.get(spec, build))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.wait_for(second, 1) is client
        assert build.call_count == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await owner.aclose()
    client.aclose.assert_awaited_once()


async def test_failed_client_setup_can_retry():
    from unittest.mock import Mock

    owner = AsyncTaskClients()
    client = SimpleNamespace(aclose=AsyncMock())
    build = Mock(side_effect=[OSError("temporary failure"), client])
    spec = {"url": "http://test.invalid"}
    try:
        with pytest.raises(OSError, match="temporary"):
            await owner.get(spec, build)
        assert await owner.get(spec, build) is client
    finally:
        await owner.aclose()


async def test_client_cleanup_cannot_freeze_shutdown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("novacode_cli.remote.async_task_clients._CLOSE_TIMEOUT", 0.01)
    owner = AsyncTaskClients()
    released = asyncio.Event()

    async def close() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    client = SimpleNamespace(aclose=close)
    await owner.get({"url": "http://test.invalid"}, lambda: client)
    await asyncio.wait_for(owner.aclose(), 0.5)
    assert released.is_set()
    assert not owner._clients


async def test_waiter_budget_includes_slow_discovery(monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()

    def discover() -> list:
        release.wait(2)
        return []

    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        discover,
    )
    try:
        async with asyncio.timeout(1.5):
            result = await waiter_tools._wait_remote(
                {
                    "task_id": "task",
                    "agent_name": "test",
                    "thread_id": "thread",
                },
                1,
            )
        assert result["timed_out"]
        assert not waiter_tools.active_waiters()
    finally:
        release.set()


async def test_waiter_keeps_completed_status_when_result_fetch_expires(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        lambda: [{"name": "test", "url": "http://test.invalid"}],
    )
    client = SimpleNamespace(
        runs=SimpleNamespace(get=AsyncMock(return_value={"status": "success"})),
        threads=SimpleNamespace(get=AsyncMock(side_effect=lambda **_kwargs: None)),
        aclose=AsyncMock(),
    )

    async def get_thread(**_kwargs: object) -> dict:
        await asyncio.Event().wait()
        return {}

    client.threads.get = get_thread
    monkeypatch.setattr(waiter_tools, "_remote_client", lambda _spec: client)
    result = await waiter_tools._wait_remote(
        {
            "task_id": "task",
            "agent_name": "test",
            "thread_id": "thread",
            "run_id": "run",
        },
        1,
    )
    assert result["status"] == "success"
    assert "result could not be fetched" in result["result"]
    client.aclose.assert_awaited_once()


async def test_interrupting_remote_wait_closes_client_and_clears_ui(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        lambda: [{"name": "test", "url": "http://test.invalid"}],
    )
    checking = asyncio.Event()

    async def get_run(**_kwargs: object) -> dict:
        checking.set()
        await asyncio.Event().wait()
        return {}

    client = SimpleNamespace(runs=SimpleNamespace(get=get_run), aclose=AsyncMock())
    monkeypatch.setattr(waiter_tools, "_remote_client", lambda _spec: client)
    waiting = asyncio.create_task(
        waiter_tools._interruptible_wait(
            waiter_tools._wait_remote(
                {
                    "task_id": "task",
                    "agent_name": "test",
                    "thread_id": "thread",
                    "run_id": "run",
                },
                10,
            ),
            "interrupt-client-test",
        )
    )
    try:
        await asyncio.wait_for(checking.wait(), 1)
        waiter_tools.interrupt_task_waits("interrupt-client-test")
        assert await asyncio.wait_for(waiting, 0.5) is None
        assert not waiter_tools.active_waiters()
        client.aclose.assert_awaited_once()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        waiter_tools._released.pop("interrupt-client-test", None)


async def test_interrupted_session_history_stays_bounded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(waiter_tools, "_MAX_RELEASED_THREADS", 4)
    waiter_tools._released.clear()
    thread_ids = [f"session-{index}" for index in range(6)]
    waiting = [
        asyncio.create_task(
            waiter_tools._interruptible_wait(
                asyncio.Event().wait(),
                thread_id,
            )
        )
        for thread_id in thread_ids
    ]
    try:
        await asyncio.sleep(0)  # Register every wait before sending user input.
        for thread_id in thread_ids:
            waiter_tools.interrupt_task_waits(thread_id)
        assert await asyncio.wait_for(asyncio.gather(*waiting), 1) == [None] * len(thread_ids)
        assert len(waiter_tools._released) == waiter_tools._MAX_RELEASED_THREADS
        assert list(waiter_tools._released) == thread_ids[-4:]
        assert not waiter_tools._interrupts
    finally:
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        waiter_tools._released.clear()


async def test_cancelling_close_keeps_cleanup_owned():
    owner = AsyncTaskClients()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close() -> None:
        entered.set()
        await release.wait()
        finished.set()

    client = SimpleNamespace(aclose=close)
    await owner.get({"url": "http://test.invalid"}, lambda: client)
    closing = asyncio.create_task(owner.aclose())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        release.set()
        await asyncio.wait_for(owner.aclose(), 0.5)
        assert finished.is_set()
    finally:
        release.set()
        await owner.aclose()
