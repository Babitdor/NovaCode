"""ElevenLabs cloud TTS — text to speech via aiohttp, playback via sounddevice.

Requires an ElevenLabs API key (stored in config).
See https://elevenlabs.io/docs/api-reference/text-to-speech
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import TYPE_CHECKING

import aiohttp

from novacode_cli.audio.playback import play_and_wait, run_speech

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("nova.audio.stt_elevenlabs")

_ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel
_STATUS_OK = 200


class ElevenLabsSpeaker:
    """Synthesises text via ElevenLabs and plays through sounddevice."""

    def __init__(self, *, api_key: str = "", voice_id: str = _DEFAULT_VOICE) -> None:
        """Store the API key and voice ID."""
        self._api_key = api_key
        self._voice_id = voice_id

    async def speak(self, text: str) -> None:
        """Send text to ElevenLabs and play the returned audio."""
        if not self._api_key:
            msg = (
                "ElevenLabs API key is not set. Configure with: "
                "/voice settings tts elevenlabs --key <key>"
            )
            raise RuntimeError(msg)
        if not text.strip():
            return

        url = f"{_ELEVENLABS_BASE}/text-to-speech/{self._voice_id}"
        headers = {
            "Accept": "audio/mpeg",
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
        }

        async with (
            aiohttp.ClientSession(headers=headers) as session,
            session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp,
        ):
            if resp.status != _STATUS_OK:
                err = await resp.text()
                api_msg = f"ElevenLabs API error ({resp.status}): {err[:200]}"
                raise RuntimeError(api_msg)
            mp3_data = await resp.read()

        proc = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-i", "-", "-f", "s16le", "-ar", "22050", "-ac", "1", "-"],
            input=mp3_data,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace")[:200]
            fmsg = f"ffmpeg decode failed: {err}"
            raise RuntimeError(fmsg)

        import numpy as np

        pcm = np.frombuffer(proc.stdout, dtype="int16")
        if len(pcm) == 0:
            return
        await run_speech(lambda: self._play_and_wait(pcm))

    def _play_and_wait(self, pcm: np.ndarray) -> None:
        play_and_wait(pcm, 22050)
