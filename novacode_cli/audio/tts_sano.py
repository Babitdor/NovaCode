"""Tiny sanoTTS voices with NumPy inference and no GPU/PyTorch dependency."""

from __future__ import annotations

from typing import Any

from novacode_cli.audio.downloads import download_file
from novacode_cli.audio.playback import play_and_wait, run_speech
from novacode_cli.audio.speakable import speech_sentences
from novacode_cli.config.config import HOME_DIR

_VOICE_DIR = HOME_DIR / "voice" / "sano"
_ASSET_URL = "https://huggingface.co/ampixa/sanoTTS/resolve/main"
_PACKAGES = {
    "amy-1p1m": "amy-en-1p1m",
    "amy": "amy-en-1p46m",
    "kristin": "kristin-en-1p4m",
    "hfc": "hfc-en-1p8m",
}
_FILES = ("manifest.json", "weights.fp16.bin", "piper-phoneme-config.json")


class SanoSpeaker:
    """Reuse a tiny English voice pack and speak each sentence as it is ready."""

    def __init__(self, *, voice: str = "amy-1p1m") -> None:
        """Select a supported voice; no imports or downloads during construction."""
        if voice not in _PACKAGES:
            msg = f"Unknown sanoTTS voice: {voice}. Choose from {', '.join(_PACKAGES)}."
            raise ValueError(msg)
        self._voice_name = voice
        self._model: Any = None

    @property
    def needs_download(self) -> bool:
        """Whether this voice is missing any required asset."""
        directory = _VOICE_DIR / _PACKAGES[self._voice_name]
        return any(not (directory / name).is_file() for name in _FILES)

    def _ensure_voice(self) -> None:
        if self._model is not None:
            return
        try:
            from sanotts import Synthesizer
        except ImportError as exc:
            from novacode_cli.audio import sano_install_hint

            raise ImportError(sano_install_hint()) from exc

        package = _PACKAGES[self._voice_name]
        directory = _VOICE_DIR / package
        for name in _FILES:
            target = directory / name
            if not target.is_file():
                download_file(f"{_ASSET_URL}/{package}/{name}", target)
        # Local path prevents vendor downloading; its loader validates weight hashes.
        self._model = Synthesizer(voice=self._voice_name, voice_dir=directory)

    async def speak(self, text: str) -> None:
        """Synthesize off-loop with shared playback ownership and cancellation."""
        if text.strip():
            await run_speech(lambda: self._speak_sync(text))

    def _speak_sync(self, text: str) -> None:
        self._ensure_voice()
        for sentence in speech_sentences(text):
            result = self._model.synthesize(sentence)
            if not play_and_wait(result.audio, result.sample_rate):
                return
