"""Session-owned HTTP clients for background task observation."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from langgraph_sdk.client import LangGraphClient

logger = logging.getLogger(__name__)
_CLOSE_TIMEOUT = 1.0
ClientKey = tuple[str | None, tuple[tuple[str, str], ...]]


class AsyncTaskClients:
    """Coalesce route-specific setup off the UI loop and own its cleanup.

    Cancelling an observation cannot stop a worker thread constructing a client.
    The initialization task remains owned until it can close that client, even
    if this owner has already closed. Clients never cross session event loops.
    """

    def __init__(self) -> None:
        """Start with no HTTP resources or executor work."""
        self._clients: dict[ClientKey, asyncio.Task[LangGraphClient | None]] = {}
        self._closed = False
        self._closing: asyncio.Future[list[None | BaseException]] | None = None

    async def get(
        self,
        spec: Mapping[str, Any],
        create: Callable[[], LangGraphClient],
    ) -> LangGraphClient:
        """Share one client per URL and authorization headers within this owner."""
        if self._closed:
            message = "Background task clients are closed"
            raise RuntimeError(message)
        key = (spec.get("url"), tuple(sorted((spec.get("headers") or {}).items())))
        pending = self._clients.get(key)
        if pending is None:
            pending = asyncio.create_task(self._create(create))
            self._clients[key] = pending
            pending.add_done_callback(lambda task: self._setup_done(key, task))
        client = await asyncio.shield(pending)
        if client is None or self._closed:
            message = "Background task clients are closed"
            raise RuntimeError(message)
        return client

    async def _create(self, create: Callable[[], LangGraphClient]) -> LangGraphClient | None:
        client = await asyncio.to_thread(create)
        if self._closed:
            await self._close(client)
            return None
        return client

    def _setup_done(self, key: ClientKey, task: asyncio.Task[LangGraphClient | None]) -> None:
        failed = task.cancelled() or task.exception() is not None
        if (self._closed or failed) and self._clients.get(key) is task:
            self._clients.pop(key, None)

    @staticmethod
    async def _close(client: LangGraphClient) -> None:
        close = getattr(client, "aclose", None)
        if close is None:
            return
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await close()
        except Exception:  # noqa: BLE001 — cleanup must not stall task completion.
            logger.debug("Background task client cleanup failed", exc_info=True)

    async def aclose(self) -> None:
        """Close ready clients; unfinished setup closes itself without delaying exit."""
        if self._closed:
            if self._closing is not None:
                await asyncio.shield(self._closing)
            return
        self._closed = True
        ready = []
        for key, pending in tuple(self._clients.items()):
            if pending.done():
                self._clients.pop(key, None)
                if not pending.cancelled() and pending.exception() is None:
                    client = pending.result()
                    if client is not None:
                        ready.append(client)
        if ready:
            self._closing = asyncio.gather(
                *(self._close(client) for client in ready),
                return_exceptions=True,
            )
            await asyncio.shield(self._closing)
