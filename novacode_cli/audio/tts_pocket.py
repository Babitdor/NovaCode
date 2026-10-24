"""Pocket-TTS text-to-speech wrapper.

Synthesises speech with Kyutai Pocket-TTS (CPU optimized, low latency) and
plays it through sounddevice. Imports cleanly without `pocket-tts` installed.
"""

from __future__ import annotations

import logging
from typing import Any

from novacode_cli.audio.playback import play_and_wait, run_speech

logger = logging.getLogger("nova.audio.tts_pocket")


class PocketSpeaker:
    """Synthesises and plays speech with Kyutai Pocket TTS."""

    def __init__(self, *, voice: str = "alba") -> None:
        """Configure the voice; the model loads/downloads on first use."""
        self._voice_name = voice or "alba"
        self._model: Any = None
        self._voice_state: Any = None
        self._samplerate = 24_000  # Default fallback, updated on model load

    @property
    def needs_download(self) -> bool:
        """Return whether the voice model files are missing and need to be downloaded."""
        from pathlib import Path
        import os

        hf_home = os.environ.get("HF_HOME") or os.path.expanduser(
            "~/.cache/huggingface"
        )
        hub = Path(hf_home) / "hub"
        # Match whatever kyutai/pocket-tts* repo this pocket_tts version pulls
        # (the real cache dir is `models--kyutai--pocket-tts-without-voice-cloning`).
        # The old fixed `models--kyutai--pocket-tts` never matched, so the
        # "downloading voice…" hint showed on every speak despite a cached model.
        try:
            return not any(hub.glob("models--kyutai--pocket-tts*"))
        except OSError:
            return False

    def _ensure_voice(self) -> None:
        if self._model is None:
            from pocket_tts import TTSModel

            logger.info(
                "Loading Pocket-TTS (Kyutai) — this may download weights from Hugging Face"
            )
            self._model = TTSModel.load_model()
            self._samplerate = getattr(self._model, "sample_rate", self._samplerate)

            # Resolve the voice state
            from pathlib import Path

            voice_path = Path(self._voice_name)
            if voice_path.exists() and voice_path.suffix.lower() == ".wav":
                logger.info("Cloning custom voice from: %s", self._voice_name)
                self._voice_state = self._model.get_state_for_audio_prompt(
                    str(voice_path)
                )
            else:
                # Built-in voice name (default "alba")
                name = self._voice_name.lower().strip()
                known_voices = {
                    "alba",
                    "giovanni",
                    "lola",
                    "juergen",
                    "rafael",
                    "estelle",
                    "anna",
                    "azelma",
                    "bill_boerst",
                    "caro_davy",
                    "charles",
                    "cosette",
                    "eponine",
                    "eve",
                    "fantine",
                    "george",
                    "jane",
                    "jean",
                    "javert",
                    "marius",
                    "mary",
                    "michael",
                    "paul",
                    "peter_yearsley",
                    "stuart_bell",
                    "vera",
                }
                if name not in known_voices:
                    logger.warning(
                        "Unknown Pocket-TTS voice: %s, falling back to 'alba'",
                        self._voice_name,
                    )
                    name = "alba"
                self._voice_state = self._model.get_state_for_audio_prompt(name)

    async def speak(self, text: str) -> None:
        """Synthesise ``text`` and play it (off the event loop). No-op if empty."""
        if not text.strip():
            return
        await run_speech(lambda: self._speak_sync(text))

    def _speak_sync(self, text: str) -> None:
        import numpy as np

        self._ensure_voice()
        # Generate audio using the model
        audio = self._model.generate_audio(self._voice_state, text)
        if audio is None:
            return

        # Convert to numpy array
        if hasattr(audio, "numpy"):
            pcm = audio.numpy()
        elif hasattr(audio, "cpu"):
            pcm = audio.cpu().numpy()
        else:
            pcm = np.asarray(audio)

        pcm = pcm.reshape(-1)
        if len(pcm) == 0:
            return

        play_and_wait(pcm, self._samplerate)
