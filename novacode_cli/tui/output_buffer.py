"""Bounded display-only output tails; callers synchronize producers and drains."""

from __future__ import annotations

from collections import deque

MAX_PENDING_CALLS = 32
MAX_PENDING_CHARS = 65_536


class OutputTail:
    """Keep recent chunks without retaining a command's entire output burst."""

    def __init__(self) -> None:
        """Start an empty tail with no truncation notice."""
        self._chunks: deque[str] = deque()
        self._size = 0
        self._truncated = False

    def __len__(self) -> int:
        """Return the number of retained characters."""
        return self._size

    def append(self, text: str) -> None:
        """Keep at most MAX_PENDING_CHARS, retaining the newest output."""
        if not text:
            return
        if len(text) > MAX_PENDING_CHARS:
            text = text[-MAX_PENDING_CHARS:]
            self._truncated = True
        self._chunks.append(text)
        self._size += len(text)
        while self._size > MAX_PENDING_CHARS:
            excess = self._size - MAX_PENDING_CHARS
            oldest = self._chunks.popleft()
            removed = min(excess, len(oldest))
            self._size -= removed
            if removed < len(oldest):
                self._chunks.appendleft(oldest[removed:])
            self._truncated = True

    def peek(self) -> str:
        """Read the bounded display tail without consuming it."""
        text = "".join(self._chunks)
        if self._truncated:
            text = "[live output truncated; showing recent output]\n" + text
        return text

    def drain(self) -> str:
        """Return the bounded text, with an explicit display truncation marker."""
        text = self.peek()
        self.clear()
        return text

    def clear(self) -> None:
        """Release chunks without allocating a joined string."""
        self._chunks.clear()
        self._size = 0
        self._truncated = False
