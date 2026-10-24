"""Lazy, CPU-only Whistle speech recognition through the Cactus runtime."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

# Whistle's native model is process-global, including across Python instances.
_RUNTIME_LOCK = threading.RLock()


class WhistleTranscriber:
    """Transcribe Nova's 16 kHz int16 audio without blocking the event loop."""

    def __init__(self, *, model: str = "whistle", language: str | None = "en") -> None:
        """Select the published Whistle checkpoint; defer native loading."""
        if model != "whistle":
            msg = "Whistle supports the 'whistle' model."
            raise ValueError(msg)
        if language and language not in {"en", "de", "fr", "es", "it", "nl", "pl"}:
            msg = f"Unsupported Whistle language: {language}"
            raise ValueError(msg)
        self._language = language or None
        self._model: Any = None

    @property
    def needs_download(self) -> bool:
        """Probe only the model cache; never trigger a download from the UI."""
        try:
            from huggingface_hub import try_to_load_from_cache

            cached = try_to_load_from_cache("Cactus-Compute/whistle", "whistle.cact")
        except Exception:  # noqa: BLE001 -- best-effort progress hint, like Faster-Whisper.
            return False
        return not isinstance(cached, str)

    def _ensure_model(self) -> None:
        """Load under the same lock used for all native transcription calls."""
        with _RUNTIME_LOCK:
            if self._model is None:
                from needle import Whistle

                self._model = Whistle()

    async def transcribe(self, pcm_int16: np.ndarray) -> str:
        """Return plain text, converting int16 samples to normalized float32."""
        if not len(pcm_int16):
            return ""
        return await asyncio.to_thread(self._transcribe_sync, pcm_int16)

    def _transcribe_sync(self, pcm_int16: np.ndarray) -> str:
        with _RUNTIME_LOCK:
            self._ensure_model()
            audio = pcm_int16.astype("float32") / 32768.0
            result = self._model.transcribe(audio, language=self._language)
            text = result.get("text", "")
            return text.strip() if isinstance(text, str) else ""
