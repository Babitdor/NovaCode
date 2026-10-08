"""Authenticated loopback hub: one Telegram poller shared by Nova processes.

The listening socket elects the owner atomically. Clients reconnect and elect a
new owner when that process exits; only the owner calls getUpdates. No daemon,
extra package, bot token on disk, or externally reachable port is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiohttp import web

FetchUpdates = Callable[[int], Awaitable[list[dict[str, Any]]]]


class TelegramHub:
    def __init__(self, auth: str, fetch: FetchUpdates, offset_store: Any, key: str) -> None:
        self.auth, self.fetch, self.store, self.key = auth, fetch, offset_store, key
        self.clients: dict[str, web.WebSocketResponse] = {}
        self.topics: dict[tuple[int, int], str] = {}
        self.chats: dict[int, set[str]] = {}
        self.sessions: dict[tuple[int, str], str] = {}
        self.runner: web.AppRunner | None = None
        self.task: asyncio.Task[None] | None = None

    async def start(self, port: int) -> None:
        app = web.Application(client_max_size=65536)
        app.router.add_get("/telegram", self.connect)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", port).start()
        except BaseException:
            await self.runner.cleanup()
            raise
        self.task = asyncio.create_task(self.poll(), name="telegram-shared-poller")

    async def connect(self, request: web.Request) -> web.WebSocketResponse:
        if not hmac.compare_digest(request.headers.get("Authorization", ""), self.auth):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=65536)
        await ws.prepare(request)
        client = uuid.uuid4().hex
        self.clients[client] = ws
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                bindings = {(int(chat), int(topic)) for chat, topic in data.get("topics", [])}
                sessions = {(int(chat), str(sid)) for chat, sid in data.get("sessions", [])}
                if any(key in self.sessions and self.sessions[key] != client for key in sessions):
                    await ws.send_json(
                        {"error": "This Nova session is already connected in another window."}
                    )
                    continue
                if any(
                    key[1] and key in self.topics and self.topics[key] != client for key in bindings
                ):
                    await ws.send_json(
                        {"error": "This Telegram topic is already owned by another Nova window."}
                    )
                    continue
                self.topics = {key: value for key, value in self.topics.items() if value != client}
                self.topics.update({key: client for key in bindings if key[1]})
                self.sessions = {
                    key: value for key, value in self.sessions.items() if value != client
                }
                self.sessions.update({key: client for key in sessions})
                for clients in self.chats.values():
                    clients.discard(client)
                for chat, _ in bindings:
                    self.chats.setdefault(chat, set()).add(client)
                await ws.send_json({"ready": True})
        finally:
            self.clients.pop(client, None)
            self.sessions = {key: value for key, value in self.sessions.items() if value != client}
            self.topics = {key: value for key, value in self.topics.items() if value != client}
            for clients in self.chats.values():
                clients.discard(client)
        return ws

    async def poll(self) -> None:
        try:
            await self._poll()
        finally:
            for ws in list(self.clients.values()):
                await ws.close()

    async def _poll(self) -> None:
        item = await self.store.aget(("nova", "telegram_offsets"), self.key)
        offset = int(item.value.get("offset", 0)) if item else 0
        while True:
            # Don't acknowledge updates while no sessions have subscribed.
            if not self.clients:
                await asyncio.sleep(0.5)
                continue
            updates = await self.fetch(offset)
            for update in updates:
                message = update.get("message", {})
                key = (message.get("chat", {}).get("id"), message.get("message_thread_id"))
                client = self.topics.get(key)
                if client is None and key[1] in (None, 1):
                    candidates = self.chats.get(key[0], set())
                    client = next(iter(candidates)) if len(candidates) == 1 else None
                ws = self.clients.get(client or "")
                if ws is not None and not ws.closed:
                    await ws.send_json({"updates": [update]})
                offset = max(offset, int(update.get("update_id", 0)) + 1)
            if updates:
                await self.store.aput(("nova", "telegram_offsets"), self.key, {"offset": offset})
            else:
                await asyncio.sleep(0.5)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        for ws in list(self.clients.values()):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()


class SharedTelegramPolling:
    def __init__(self, token: str, fetch: FetchUpdates) -> None:
        self.key = hashlib.sha256(token.encode()).hexdigest()
        self.auth = "Bearer " + hashlib.sha256(("nova-telegram-hub:" + token).encode()).hexdigest()
        self.port = 20000 + int(self.key[:8], 16) % 40000
        self.fetch = fetch
        self.hub: TelegramHub | None = None
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.bindings: list[tuple[int, int]] = []
        self.sessions: set[tuple[int, str]] = set()
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=100)
        self.reader: asyncio.Task[None] | None = None
        self.ready: asyncio.Future[None] | None = None
        self.lock = asyncio.Lock()

    async def _connect(self) -> None:
        from novacode_cli.memory.store import get_durable_store

        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        if self.ws is not None and not self.ws.closed:
            return
        if self.hub is not None:
            await self.hub.close()
            self.hub = None
        hub = TelegramHub(
            self.auth, self.fetch, await asyncio.to_thread(get_durable_store), self.key
        )
        try:
            await hub.start(self.port)
            self.hub = hub
        except OSError:
            # Another Nova process owns the socket; authentication confirms it
            # is the same bot rather than trusting a service on a guessed port.
            pass
        self.ws = await self.session.ws_connect(
            f"http://127.0.0.1:{self.port}/telegram",
            headers={"Authorization": self.auth},
            heartbeat=10,
        )
        self.reader = asyncio.create_task(self._read(self.ws), name="telegram-hub-reader")
        await self._subscribe()

    async def _subscribe(self) -> None:
        assert self.ws is not None
        self.ready = asyncio.get_running_loop().create_future()
        await self.ws.send_json({"topics": self.bindings, "sessions": list(self.sessions)})
        await asyncio.wait_for(self.ready, timeout=5)

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                if self.ready is not None and not self.ready.done():
                    if data.get("error"):
                        self.ready.set_exception(RuntimeError(data["error"]))
                    elif data.get("ready"):
                        self.ready.set_result(None)
                for update in data.get("updates", []):
                    await self.queue.put(update)
        finally:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(ConnectionError("Telegram hub disconnected"))
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)

    async def subscribe(self, bindings: list[tuple[int, int]]) -> None:
        async with self.lock:
            self.bindings = bindings
            connected = self.ws is not None and not self.ws.closed
            await self._connect()
            if connected:
                await self._subscribe()

    async def next_updates(self) -> list[dict[str, Any]]:
        while True:
            try:
                async with self.lock:
                    await self._connect()
                update = await self.queue.get()
                if update is not None:
                    return [update]
            except (aiohttp.ClientError, OSError, TimeoutError):
                await asyncio.sleep(1)

    async def reserve_session(self, chat: int | str, session_id: str) -> None:
        async with self.lock:
            await self._connect()
            key = (int(chat), session_id)
            self.sessions.add(key)
            try:
                await self._subscribe()
            except BaseException:
                self.sessions.discard(key)
                raise

    async def release_session(
        self, chat: int | str, session_id: str, bindings: list[tuple[int, int]]
    ) -> None:
        async with self.lock:
            self.sessions.discard((int(chat), session_id))
            self.bindings = bindings
            connected = self.ws is not None and not self.ws.closed
            await self._connect()
            if connected:
                await self._subscribe()

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.ws:
            await self.ws.close()
        if self.hub:
            await self.hub.close()
        if self.session:
            await self.session.close()
