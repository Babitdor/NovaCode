"""Real loopback connections exercise shared polling, isolation and handoff."""

import asyncio
import uuid

import aiohttp
import pytest
from langgraph.store.memory import InMemoryStore

from novacode_cli.remote.telegram_hub import SharedTelegramPolling, TelegramHub


@pytest.mark.asyncio
async def test_hub_authentication_rejects_other_clients():
    hub = TelegramHub("Bearer secret", None, InMemoryStore(), "test")
    await hub.start(0)
    try:
        async with aiohttp.ClientSession() as session:
            port = hub.runner.addresses[0][1]
            with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                await session.ws_connect(
                    f"http://127.0.0.1:{port}/telegram", headers={"Authorization": "Bearer wrong"}
                )
            assert error.value.status == 403
            assert not hub.clients
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_shared_polling_routes_two_sessions_and_hands_off(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    incoming = asyncio.Queue()
    pollers = []

    async def fetch_a(offset):
        pollers.append(("a", offset))
        return [await incoming.get()]

    async def fetch_b(offset):
        pollers.append(("b", offset))
        return [await incoming.get()]

    token = "test-" + uuid.uuid4().hex
    a, b = SharedTelegramPolling(token, fetch_a), SharedTelegramPolling(token, fetch_b)
    try:
        await a.reserve_session(7, "session-alpha")
        await a.subscribe([(7, 0), (7, 55), (7, 1)])
        await b.reserve_session(7, "session-beta")
        await b.subscribe([(7, 0), (7, 66)])
        assert a.hub is not None and b.hub is None
        for mid, topic in [(1, 55), (2, 66)]:
            await incoming.put(
                {
                    "update_id": mid,
                    "message": {"chat": {"id": 7}, "message_thread_id": topic, "text": str(mid)},
                }
            )
        first = await asyncio.wait_for(a.next_updates(), 5)
        second = await asyncio.wait_for(b.next_updates(), 5)
        assert first[0]["message"]["message_thread_id"] == 55
        assert second[0]["message"]["message_thread_id"] == 66
        await incoming.put({"update_id": 2, "message": {"chat": {"id": 7}, "message_thread_id": 1}})
        assert (await asyncio.wait_for(a.next_updates(), 5))[0]["message"]["message_thread_id"] == 1
        assert all(name == "a" for name, _ in pollers)
        await a.close()
        await incoming.put(
            {
                "update_id": 3,
                "message": {"chat": {"id": 7}, "message_thread_id": 66, "text": "after handoff"},
            }
        )
        assert (await asyncio.wait_for(b.next_updates(), 5))[0]["update_id"] == 3
        assert b.hub is not None
        assert any(name == "b" and offset == 3 for name, offset in pollers)
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_same_session_cannot_be_claimed_by_two_windows(monkeypatch):
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", InMemoryStore)

    async def fetch(offset):
        await asyncio.sleep(10)
        return []

    token = "test-" + uuid.uuid4().hex
    a, b = SharedTelegramPolling(token, fetch), SharedTelegramPolling(token, fetch)
    try:
        await a.reserve_session(7, "same-session")
        with pytest.raises(RuntimeError, match="already connected"):
            await b.reserve_session(7, "same-session")
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_topic_release_and_ambiguous_chat_never_reach_another_session(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    incoming = asyncio.Queue()

    async def fetch(offset):
        return [await incoming.get()]

    token = "test-" + uuid.uuid4().hex
    a, b = SharedTelegramPolling(token, fetch), SharedTelegramPolling(token, fetch)
    try:
        await a.reserve_session(7, "alpha")
        await a.subscribe([(7, 0), (7, 55)])
        await b.reserve_session(7, "beta")
        await b.subscribe([(7, 0), (7, 66)])
        with pytest.raises(RuntimeError, match="already owned"):
            await b.subscribe([(7, 0), (7, 55)])
        await b.subscribe([(7, 0), (7, 66)])
        await a.release_session(7, "alpha", [(7, 0)])
        for mid, topic in [(1, None), (2, 55), (3, 99), (4, 66)]:
            message = {"chat": {"id": 7}, "text": str(mid)}
            if topic is not None:
                message["message_thread_id"] = topic
            await incoming.put({"update_id": mid, "message": message})
        assert (await asyncio.wait_for(b.next_updates(), 5))[0]["update_id"] == 4
        assert a.queue.empty()
        await b.reserve_session(7, "alpha")
    finally:
        await a.close()
        await b.close()
