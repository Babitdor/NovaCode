"""Bounded display-only output tails, free of any UI framework.

The live preview and the Textual output pane both need "keep the newest N
characters of a command's burst". That rule lives here rather than in
`novacode_cli/tui/output_buffer.py` so the socket server and the headless path
can read it without importing Textual (ADR-003). The TUI module re-exports
these names, so both frontends share one implementation.

Chunks may be plain strings or pre-parsed ``rich.text.Text`` objects: the TUI
parses ANSI escape codes on its producer thread so the UI loop never runs
``Text.from_ansi`` over a burst. Plain-string consumers keep working through
``peek``/``drain``.
"""

from __future__ import annotations

from collections import deque

from rich.text import Text

MAX_PENDING_CALLS = 32
MAX_PENDING_CHARS = 65_536

Chunk = str | Text

_TRUNCATION_MARKER = "[live output truncated; showing recent output]\n"


def _plain(chunk: Chunk) -> str:
    return chunk.plain if isinstance(chunk, Text) else chunk


class OutputTail:
    """Keep recent chunks without retaining a command's entire output burst."""

    def __init__(self) -> None:
        """Start an empty tail with no truncation notice."""
        self._chunks: deque[Chunk] = deque()
        self._size = 0
        self._truncated = False

    def __len__(self) -> int:
        """Return the number of retained characters."""
        return self._size

    def append(self, text: Chunk) -> None:
        """Keep at most MAX_PENDING_CHARS, retaining the newest output."""
        if not _plain(text):
            return
        plain_len = len(_plain(text))
        if plain_len > MAX_PENDING_CHARS:
            text = text[-MAX_PENDING_CHARS:]
            self._truncated = True
            plain_len = len(_plain(text))
        self._chunks.append(text)
        self._size += plain_len
        while self._size > MAX_PENDING_CHARS:
            excess = self._size - MAX_PENDING_CHARS
            oldest = self._chunks.popleft()
            removed = min(excess, len(_plain(oldest)))
            self._size -= removed
            if removed < len(_plain(oldest)):
                self._chunks.appendleft(oldest[removed:])
            self._truncated = True

    def peek(self) -> str:
        """Read the bounded display tail without consuming it."""
        text = "".join(_plain(chunk) for chunk in self._chunks)
        if self._truncated:
            text = _TRUNCATION_MARKER + text
        return text

    def drain(self) -> str:
        """Return the bounded text, with an explicit display truncation marker."""
        text = self.peek()
        self.clear()
        return text

    def drain_content(self) -> Chunk:
        """Return the bounded tail, preserving pre-parsed styling when present.

        Plain-string tails return ``str`` exactly like ``drain``; tails holding
        pre-parsed chunks return a joined ``Text`` so the UI loop can skip
        ``Text.from_ansi`` entirely.
        """
        if not self._chunks:
            return ""
        if not any(isinstance(chunk, Text) for chunk in self._chunks):
            return self.drain()
        parts: list[Text] = []
        if self._truncated:
            parts.append(Text(_TRUNCATION_MARKER))
        parts.extend(chunk if isinstance(chunk, Text) else Text(chunk) for chunk in self._chunks)
        self.clear()
        joined = parts[0]
        for part in parts[1:]:
            joined += part
        return joined

    def clear(self) -> None:
        """Release chunks without allocating a joined string."""
        self._chunks.clear()
        self._size = 0
        self._truncated = False
