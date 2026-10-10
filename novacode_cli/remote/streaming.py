"""Coalesced answer streaming and editable pages, separate from tool activity."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable

from novacode_cli import ui_events as ev

logger = logging.getLogger(__name__)


def connected_editor(bridge, editor):
    """Stopping /remote prevents a captured local stream from reconnecting it."""

    async def edit(text, final=False):
        if not bridge.is_connected:
            raise RuntimeError("Remote bridge has been stopped")
        await editor(text, final)

    return edit


class PagedEditor:
    """Reconcile the entire rendered message, including shrinking/replaced text."""

    def __init__(self, render: Callable, send: Callable, edit: Callable, delete: Callable):
        self.render, self.send, self.edit, self.delete = render, send, edit, delete
        self.pages: list[tuple[object, str]] = []
        self.lock = asyncio.Lock()

    async def __call__(self, text: str, final: bool = False) -> None:
        async with self.lock:
            desired = await asyncio.to_thread(self.render, text or "…")
            for index, body in enumerate(desired):
                if index >= len(self.pages):
                    message = await self.send(body)
                    self.pages.append((message, body))
                else:
                    message, previous = self.pages[index]
                    if previous != body:
                        await self.edit(message, body)
                        self.pages[index] = (message, body)
            while len(self.pages) > len(desired):
                message, _ = self.pages[-1]
                await self.delete(message)
                self.pages.pop()


class RemoteAnswerStream:
    """Network work runs in one background pump, never in the token renderer."""

    def __init__(self, edit: Callable, *, interval: float = 1.3):
        self.edit = edit
        self.interval = interval
        self._parts: list[str] = []
        self._characters = 0
        self.text = ""
        self.dirty = False
        self.task: asyncio.Task | None = None
        self.closed = False
        self.error: str | None = None
        self.cancelled = False
        self.truncated = False

    @property
    def text(self) -> str:
        if len(self._parts) > 1:
            self._parts = ["".join(self._parts)]
        return self._parts[0] if self._parts else ""

    @text.setter
    def text(self, value: str) -> None:
        self._parts = [value] if value else []
        self._characters = len(value)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._pump())

    def feed(self, event: object) -> None:
        if self.closed:
            return
        if isinstance(event, ev.TextDelta):
            if self._characters + len(event.text) > 256_000:
                self.truncated = True
            if self._characters < 256_000:
                part = event.text[: 256_000 - self._characters]
                self._parts.append(part)
                self._characters += len(part)
            self.dirty = True
        elif isinstance(event, ev.AssistantMessage):
            self.text = event.text[:256_000]
            self.truncated = len(event.text) > 256_000
            self.dirty = True
        elif isinstance(event, (ev.TextDiscard, ev.ToolCall)):
            # Earlier prose was commentary. The next answer replaces it.
            self.text = ""
            self.truncated = False
            self.dirty = False
        elif isinstance(event, (ev.Error, ev.ContextOverflow)):
            self.error = event.message
        elif isinstance(event, ev.Cancelled):
            self.cancelled = True

    async def _pump(self) -> None:
        retry_delay = self.interval
        try:
            while True:
                await asyncio.sleep(retry_delay)
                if self.dirty and self.text.strip():
                    self.dirty = False
                    try:
                        await asyncio.wait_for(self.edit(self.text, False), timeout=15)
                        retry_delay = self.interval
                    except Exception:
                        self.dirty = True
                        retry_delay = min(max(self.interval, retry_delay * 2), 30.0)
                        retry_delay += random.uniform(0.0, min(1.0, retry_delay * 0.1))
                        logger.debug(
                            "Remote answer edit failed; retrying latest text", exc_info=True
                        )
        except asyncio.CancelledError:
            return

    async def finalize(self, text: str | None = None) -> bool:
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        final = text if text is not None else self.text
        if text is None and self.truncated:
            final += "\n\n… (streamed output exceeded the 256,000-character buffer)."
        if self.error:
            final = f"❌ **Task failed**\n\n{self.error}"
        elif self.cancelled:
            final = f"⏹ **Task stopped**\n\n{final}".strip()
        final = final or "✅ Task completed."
        try:
            await asyncio.wait_for(self.edit(final, True), timeout=15)
            return True
        except Exception:
            logger.debug("Final remote answer edit failed", exc_info=True)
            return False
