"""Read noisy subprocess output in bounded batches while yielding to input."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator


async def shell_output_batches(stream: asyncio.StreamReader) -> AsyncIterator[list[str]]:
    """Handle split UTF-8 and long newline-free output without readline limits."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while True:
        chunk = await stream.read(16384)
        pending += decoder.decode(chunk, final=not chunk)
        lines = pending.split("\n")
        pending = lines.pop()
        # Some commands print a single enormous line. Segment it for display
        # rather than accumulating it or exceeding StreamReader.readline's cap.
        while len(pending) > 16384:
            lines.append(pending[:16384])
            pending = pending[16384:]
        if not chunk and pending:
            lines.append(pending)
        if lines:
            yield [line.rstrip("\r") for line in lines]
        if not chunk:
            break
        # Buffered pipe reads may finish immediately for thousands of chunks.
        # Explicitly let keyboard input, cancellation and repaint tasks run.
        await asyncio.sleep(0)
