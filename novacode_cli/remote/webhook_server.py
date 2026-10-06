"""Webhook ingress server — Loop-Engineering Enhancement 5 (event-driven).

A small ``aiohttp`` server that lets external systems (GitHub, Linear, or any
signed sender) trigger a Nova run without a human relaying the message through
Discord/Telegram. It accepts ``POST /webhook/{source}``, hands the raw request
to the matching adapter in :mod:`novacode_cli.remote.webhook_adapters` (which
verifies the signature), and on success puts the resulting
:class:`~novacode_cli.remote.bridge.RemoteMessage` on the shared queue — the
same queue the bridges and cron scheduler feed.

Security
--------
- Per-source secrets + allowed event types live in the durable store
  (:data:`config.WEBHOOK_CONFIG_NS`), not in env vars.
- Signatures are verified inside the adapters (timing-safe).
- Binds to ``127.0.0.1`` by default; exposing it publicly is an explicit choice.
- Unregistered sources and bad signatures get an opaque 401 (no detail leaked).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from novacode_cli.events import cap_event_log, nova_event_log
from novacode_cli.hermes import config
from novacode_cli.remote.webhook_adapters import ADAPTERS

if TYPE_CHECKING:
    import asyncio

    from langgraph.store.base import BaseStore

    from novacode_cli.remote.bridge import RemoteMessage

logger = logging.getLogger("nova.remote.webhook_server")

_DEFAULT_PORT = 9876


class WebhookServer:
    """Serves ``POST /webhook/{source}`` and enqueues verified payloads.

    Args:
        queue: Shared queue the remote processor consumes.
        store: Durable store holding per-source secrets / allowed events.
        host: Bind address (``127.0.0.1`` by default — local only).
        port: Bind port.
    """

    def __init__(
        self,
        queue: asyncio.Queue[RemoteMessage],
        *,
        store: BaseStore | None = None,
        host: str = "127.0.0.1",
        port: int = _DEFAULT_PORT,
    ) -> None:
        """Hold deps; the server is inert until :meth:`start`."""
        self._queue = queue
        self._store = store
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    @property
    def running(self) -> bool:
        """Whether the HTTP server is currently bound and serving."""
        return self._runner is not None

    @property
    def url(self) -> str:
        """The base URL the server listens on."""
        return f"http://{self._host}:{self._port}/webhook/{{source}}"

    async def start(self) -> None:
        """Bind and start serving (idempotent)."""
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_post("/webhook/{source}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._runner = runner
        logger.info("Webhook server listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        """Stop serving and release the socket."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def register_source(
        self, source: str, secret: str, *, event_types: list[str] | None = None
    ) -> None:
        """Persist a source's secret + allowed event types to the store."""
        if self._store is None:
            return
        await self._store.aput(
            config.WEBHOOK_CONFIG_NS,
            source,
            {"secret": secret, "event_types": event_types or []},
        )

    async def _source_config(self, source: str) -> dict[str, Any] | None:
        if self._store is None:
            return None
        try:
            entry = await self._store.aget(config.WEBHOOK_CONFIG_NS, source)
        except Exception:
            logger.exception("Failed to read webhook config for %s", source)
            return None
        if entry is not None and isinstance(entry.value, dict):
            return dict(entry.value)
        return None

    async def _handle(self, request: web.Request) -> web.Response:
        """Verify a webhook and enqueue it, or reject with an opaque status."""
        source = request.match_info.get("source", "")
        adapter = ADAPTERS.get(source)
        if adapter is None:
            return web.json_response({"status": "unknown source"}, status=404)

        cfg = await self._source_config(source)
        if not cfg or not cfg.get("secret"):
            return web.json_response({"status": "unauthorized"}, status=401)

        body = await request.read()
        allowed = cfg.get("event_types") or None
        message = adapter(
            request.headers,
            body,
            cfg["secret"],
            allowed_events=set(allowed) if allowed else None,
        )
        if message is None:
            return web.json_response({"status": "rejected"}, status=401)

        await self._queue.put(message)
        _emit_webhook_event(f"⇥ Webhook ({source}) triggered: {message.text[:80]}")
        return web.json_response({"status": "accepted"}, status=202)


def _emit_webhook_event(message: str) -> None:
    """Surface a webhook-received notice through the TUI-safe event log."""
    try:
        nova_event_log.append(("nova_webhook_received", "⇥", "cyan", message))
        cap_event_log()
    except Exception:
        logger.exception("Failed to emit webhook event")
