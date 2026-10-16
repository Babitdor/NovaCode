"""Coalesced answer streaming and editable pages, separate from tool activity."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable

from novacode_cli import ui_events as ev

logger = logging.getLogger(__name__)

#: Seconds a turn's final answer may take to reach the chat. It has to outlast
#: a rate-limit pause or two: at 15 s, a "retry after 25" lost the answer.
FINAL_DELIVERY_TIMEOUT = 120


def connected_editor(bridge, editor):
    """Stopping /remote prevents a captured local stream from reconnecting it."""

    async def edit(text, final=False):
        if not bridge.is_connected:
            raise RuntimeError("Remote bridge has been stopped")
        # A live edit during a 429 pause would only queue behind it, and the
        # queue is what made a turn's final answer miss its deadline. Skipped,
        # the pump retries with the latest text once the pause is over.
        if not final and getattr(bridge, "cooling_down", False):
            raise RuntimeError("Remote platform is rate-limiting; live edit skipped")
        await editor(text, final)

    return edit


class PagedEditor:
    """Reconcile the entire rendered message, including shrinking/replaced text.

    Empty text deletes the message: the answer stream uses it to take back
    narration that turned out not to be the answer.
    """

    def __init__(self, render: Callable, send: Callable, edit: Callable, delete: Callable):
        self.render, self.send, self.edit, self.delete = render, send, edit, delete
        self.pages: list[tuple[object, str]] = []
        self.lock = asyncio.Lock()

    async def __call__(self, text: str, final: bool = False) -> None:
        async with self.lock:
            desired = await asyncio.to_thread(self.render, text) if text else []
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
    """Network work runs in one background pump, never in the token renderer.

    Prose followed by a tool call was narration ("Let me look at…"), not the
    answer. If it was already shown, its message is deleted, so the answer
    starts a fresh message at the bottom -- after the turn's diff previews,
    rather than above them in a message the narration opened.
    """

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
        self._shown = False  # a message with streamed text exists in the chat
        self._retract = False  # that text was narration: delete the message

    @property
    def text(self) -> str:
        if len(self._parts) > 1:
            self._parts = ["".join(self._parts)]
        return self._parts[0] if self._parts else ""

    @text.setter
    def text(self, value: str) -> None:
        self._parts = [value] if value else []
        self._characters = len(value)

    def start(self, *, immediate: bool = False) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._pump(immediate=immediate))

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
            self._retract = self._retract or self._shown
        elif isinstance(event, (ev.Error, ev.ContextOverflow)):
            self.error = event.message
        elif isinstance(event, ev.Cancelled):
            self.cancelled = True

    async def _pump(self, *, immediate: bool = False) -> None:
        retry_delay = self.interval
        try:
            while True:
                await asyncio.sleep(0 if immediate else retry_delay)
                immediate = False
                if self._retract:
                    try:
                        await self._take_back()
                    except Exception:
                        logger.debug("Remote narration delete failed; retrying", exc_info=True)
                        continue
                if self.dirty and self.text.strip():
                    self.dirty = False
                    try:
                        await asyncio.wait_for(self.edit(self.text, False), timeout=15)
                        self._shown = True
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

    async def _take_back(self) -> None:
        await asyncio.wait_for(self.edit("", False), timeout=15)
        self._shown = self._retract = False

    async def finalize(self, text: str | None = None) -> bool:
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self._retract:  # the answer goes below what came after the narration
            with contextlib.suppress(Exception):
                await self._take_back()
        final = text if text is not None else self.text
        if text is None and self.truncated:
            final += "\n\n… (streamed output exceeded the 256,000-character buffer)."
        if self.error:
            final = f"❌ **Task failed**\n\n{self.error}"
        elif self.cancelled:
            final = f"⏹ **Task stopped**\n\n{final}".strip()
        final = final or "✅ Task completed."
        try:
            await asyncio.wait_for(self.edit(final, True), timeout=FINAL_DELIVERY_TIMEOUT)
            return True
        except Exception:
            logger.debug("Final remote answer edit failed", exc_info=True)
            return False
