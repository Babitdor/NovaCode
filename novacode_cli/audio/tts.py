"""Piper text-to-speech wrapper.

Synthesises speech with Piper (CPU, low latency) and plays it through
sounddevice. The voice model (``.onnx`` + ``.onnx.json``) is fetched once into
``~/.nova/voice/piper/`` on first use. Synthesis is blocking, so :meth:`Speaker.speak`
offloads to a worker thread. Imports cleanly without ``piper-tts`` installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from novacode_cli.audio.downloads import download_file as _download
from novacode_cli.audio.playback import play_and_wait, run_speech
from novacode_cli.config.config import HOME_DIR

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("nova.audio.tts")

_DEFAULT_VOICE = "en_US-lessac-medium"
_VOICE_DIR = HOME_DIR / "voice" / "piper"
#: Piper voices are published on HuggingFace under rhasspy/piper-voices.
_VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _voice_url_path(voice: str) -> str:
    """Map ``en_US-lessac-medium`` → ``en/en_US/lessac/medium/<file>`` URL stem."""
    lang_region, name, quality = voice.split("-", 2)
    lang = lang_region.split("_")[0]
    return f"{lang}/{lang_region}/{name}/{quality}/{voice}"


def ensure_voice_model(voice: str = _DEFAULT_VOICE) -> Path:
    """Return the local ``.onnx`` path for ``voice``, downloading it if missing."""
    onnx = _VOICE_DIR / f"{voice}.onnx"
    config = _VOICE_DIR / f"{voice}.onnx.json"
    stem = _voice_url_path(voice)
    if not onnx.exists():
        _download(f"{_VOICE_BASE_URL}/{stem}.onnx", onnx)
    if not config.exists():
        _download(f"{_VOICE_BASE_URL}/{stem}.onnx.json", config)
    return onnx


class Speaker:
    """Synthesises and plays speech with Piper."""

    def __init__(self, *, voice: str = _DEFAULT_VOICE) -> None:
        """Configure the voice; the model loads/downloads on first use."""
        self._voice_name = voice
        self._voice: Any = None
        self._samplerate = 22_050

    @property
    def needs_download(self) -> bool:
        """Return whether the voice model files are missing and need to be downloaded."""
        onnx = _VOICE_DIR / f"{self._voice_name}.onnx"
        config = _VOICE_DIR / f"{self._voice_name}.onnx.json"
        return not (onnx.exists() and config.exists())

    def _ensure_voice(self) -> None:
        if self._voice is None:
            from piper import PiperVoice

            model_path = ensure_voice_model(self._voice_name)
            self._voice = PiperVoice.load(str(model_path))
            self._samplerate = getattr(self._voice.config, "sample_rate", self._samplerate)

    async def speak(self, text: str) -> None:
        """Synthesise ``text`` and play it (off the event loop). No-op if empty."""
        if not text.strip():
            return
        await run_speech(lambda: self._speak_sync(text))

    def _speak_sync(self, text: str) -> None:
        self._ensure_voice()
        for chunk in self._voice.synthesize(text):
            arr = getattr(chunk, "audio_int16_array", None)
            # Piper yields complete sentences. Play before generating the rest;
            # stop consuming the generator after cancellation.
            if arr is not None and len(arr) > 0 and not play_and_wait(arr, self._samplerate):
                return
