"""Transient, bounded streaming feedback for the interactive Rich console."""

# The ui directory is an existing namespace package.
# ruff: noqa: INP001

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from rich.live import Live
from rich.text import Text

from novacode_cli.tui.output_buffer import OutputTail

if TYPE_CHECKING:
    from rich.console import Console

_PAINT_INTERVAL = 0.1


class ConsoleStreamPreview:
    """Show deltas at ten frames/second, then let final Markdown replace them."""

    def __init__(self, console: Console) -> None:
        """Keep redirected output on its existing final-message contract."""
        self.console = console
        self._tail = OutputTail()
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._last_paint = 0.0

    def _render(self) -> Text:
        with self._lock:
            return Text(self._tail.peek())

    def append(self, text: str) -> None:
        """Paint the first fragment immediately; coalesce subsequent deltas."""
        if not self.console.is_terminal or not text:
            return
        with self._lock:
            self._tail.append(text)
        if self._live is None:
            self._live = Live(
                console=self.console,
                get_renderable=self._render,
                refresh_per_second=10,
                auto_refresh=False,
                transient=True,
            )
            self._live.start(refresh=True)
            self._last_paint = time.monotonic()
        elif time.monotonic() - self._last_paint >= _PAINT_INTERVAL:
            self._live.refresh()
            self._last_paint = time.monotonic()

    def stop(self) -> None:
        """Restore the terminal and release preview data, even on interruption."""
        live, self._live = self._live, None
        try:
            if live is not None:
                live.stop()
        finally:
            with self._lock:
                self._tail.clear()
