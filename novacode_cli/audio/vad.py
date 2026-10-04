"""Silero VAD wrapper — endpoint a spoken utterance from a stream of frames.

Given 16 kHz int16 blocks, :meth:`SileroVad.collect_utterance` waits for speech
to start, accumulates it, and returns the full utterance once the speaker has
been silent for ``silence_ms``. The model is lazy-loaded so this module imports
without ``silero-vad`` / ``torch`` installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import numpy as np

logger = logging.getLogger("nova.audio.vad")

#: Silero expects 512-sample windows at 16 kHz.
_WINDOW = 512
#: Probability above which a window counts as speech.
_SPEECH_THRESHOLD = 0.5
#: Trailing silence that ends an utterance.
_DEFAULT_SILENCE_MS = 700
#: Ignore blips shorter than this so a cough isn't a "turn".
_MIN_SPEECH_MS = 200
#: Hard cap so a stuck stream can't record forever.
_MAX_UTTERANCE_MS = 30_000


class SileroVad:
    """Speech endpointer backed by the Silero VAD model."""

    def __init__(
        self,
        *,
        samplerate: int = 16_000,
        silence_ms: int = _DEFAULT_SILENCE_MS,
        threshold: float = _SPEECH_THRESHOLD,
    ) -> None:
        """Configure thresholds; the model loads on first use."""
        self._samplerate = samplerate
        self._silence_ms = silence_ms
        self._threshold = threshold
        self._model: Any = None

    def _ensure_model(self) -> None:
        if self._model is None:
            # silero_vad imports torch, which Nova keeps out of start-up. The
            # guard has to come off here or the import fails and hands-free
            # listening never gets a model ("Voice warmup failed for VAD").
            from novacode_cli._lazy_heavy import allow_transformers

            allow_transformers()
            from silero_vad import load_silero_vad

            self._model = load_silero_vad()

    async def ensure_model_async(self) -> None:
        """Load the VAD model off the event loop (idempotent; used by warmup)."""
        if self._model is None:
            import asyncio

            await asyncio.to_thread(self._ensure_model)

    def _speech_prob(self, window: np.ndarray) -> float:
        import torch

        audio = torch.from_numpy(window).float() / 32768.0
        return float(self._model(audio, self._samplerate).item())

    async def collect_utterance_async(
        self,
        read_block: Callable[[], np.ndarray | None],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> np.ndarray | None:
        """Async version of :meth:`collect_utterance` — polls via ``await asyncio.to_thread``.

        ``read_block()`` is run in a brief thread-pool call so a blocking queue
        get never stalls the event loop. The VAD inference itself runs on the
        event loop thread. This avoids the PortAudio DLL thread-affinity issue
        on Windows (the sounddevice stream is opened on the main thread and must
        be read from it, not from a long-lived thread-pool thread).
        """
        import asyncio

        # Load the model off the loop — load_silero_vad() is a blocking torch
        # call (and may download), so it must not run on the event loop thread.
        await self.ensure_model_async()
        import numpy as np

        ms_per_window = 1000 * _WINDOW / self._samplerate
        collected: list[np.ndarray] = []
        history: list[np.ndarray] = []
        max_history = 10  # keep last ~320ms of audio before speech starts
        in_speech = False
        silence_ms = 0.0
        speech_ms = 0.0
        total_ms = 0.0

        while True:
            if should_stop is not None and should_stop():
                return None
            block = await asyncio.to_thread(read_block)
            if block is None:
                continue
            for window in _windows(block, _WINDOW):
                prob = self._speech_prob(window)
                is_speech = prob >= self._threshold
                if is_speech:
                    if not in_speech:
                        collected.extend(history)
                        history.clear()
                    in_speech = True
                    speech_ms += ms_per_window
                    silence_ms = 0.0
                if in_speech:
                    collected.append(window)
                    total_ms += ms_per_window
                    if not is_speech:
                        silence_ms += ms_per_window
                    if silence_ms >= self._silence_ms or total_ms >= _MAX_UTTERANCE_MS:
                        if speech_ms < _MIN_SPEECH_MS:
                            # Too short to be a real utterance — reset and keep listening.
                            collected.clear()
                            history.clear()
                            in_speech = False
                            silence_ms = speech_ms = total_ms = 0.0
                            continue
                        return np.concatenate(collected).astype(np.int16)
                else:
                    history.append(window)
                    if len(history) > max_history:
                        history.pop(0)



def _windows(block: np.ndarray, size: int) -> Iterator[np.ndarray]:
    """Yield fixed-size windows from a block, dropping any short tail."""
    n = (len(block) // size) * size
    for i in range(0, n, size):
        yield block[i : i + size]
