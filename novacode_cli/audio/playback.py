"""Serialize native speech and prevent cancelled synthesis from playing later."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

logger = logging.getLogger("nova.audio.playback")


@dataclass
class _Speech:
    cancelled: threading.Event = field(default_factory=threading.Event)


_CURRENT: ContextVar[_Speech | None] = ContextVar("nova_speech", default=None)
_SYNTHESIS_LOCK = threading.Lock()
_OUTPUT_LOCK = threading.Lock()
_active: _Speech | None = None


def _stop_current(speech: _Speech) -> None:
    with _OUTPUT_LOCK:
        if _active is speech:
            import sounddevice as sd

            sd.stop()


async def run_speech(operation: Callable[[], None]) -> None:
    """Run blocking speech off-loop; cancellation invalidates future playback."""
    speech = _Speech()
    token = _CURRENT.set(speech)

    def run() -> None:
        # Cancelled native synthesis can still be finishing in its thread.
        # Serialize successors so they cannot re-enter the same native model.
        with _SYNTHESIS_LOCK:
            if not speech.cancelled.is_set():
                operation()

    try:
        await asyncio.to_thread(run)
    except asyncio.CancelledError:
        speech.cancelled.set()
        try:
            await asyncio.to_thread(_stop_current, speech)
        except Exception:  # noqa: BLE001 -- preserve the original cancellation.
            logger.debug("Could not stop cancelled speech", exc_info=True)
        raise
    finally:
        _CURRENT.reset(token)


def play_and_wait(pcm: np.ndarray, samplerate: int) -> bool:
    """Play owned PCM; return False if empty or cancelled, to stop further synthesis."""
    import numpy as np
    import sounddevice as sd

    global _active  # noqa: PLW0603 -- sounddevice has one process-wide convenience player.

    speech = _CURRENT.get() or _Speech()
    if not len(pcm):
        return False
    # Retain existing padding and buffering to avoid cutoff and stuttering.
    audio = np.concatenate([pcm, np.zeros(int(samplerate * 0.25), dtype=pcm.dtype)])
    with _OUTPUT_LOCK:
        if speech.cancelled.is_set():
            return False
        sd.play(audio, samplerate, latency="high")
        _active = speech
    try:
        sd.wait()
    finally:
        with _OUTPUT_LOCK:
            if _active is speech:
                _active = None
    return not speech.cancelled.is_set()
