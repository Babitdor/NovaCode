"""Turn a voice note sent from a remote bridge into text for the agent.

A voice note arrives as a compressed audio file (Telegram sends OGG/Opus), but
every STT backend in :mod:`novacode_cli.audio` speaks the same narrow dialect:
16 kHz mono int16 PCM. This module is the adapter between the two, so a spoken
message reaches the agent by exactly the path a typed one does.

Decoding goes through PyAV, which ``faster-whisper`` already depends on — no
ffmpeg binary on PATH, and no new dependency. Transcription goes through the
provider the user selected with ``/voice``, so a voice note is not quietly
handled by a different engine (or a different Whisper model) than push-to-talk.

Voice support is an optional extra (``voice``; see ``install_commands``), so
the missing-dependency path raises :class:`VoiceNotesUnavailable` with an
actionable message rather than failing obscurely — the bridge shows it to the
sender, who is not at the terminal and cannot read a traceback.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("nova.remote.voice")

#: Refuse anything longer than this. A voice note is a prompt, not a podcast:
#: local Whisper transcribes roughly in real time on CPU, so a 20-minute clip
#: would block the bridge for minutes with no feedback to the sender.
MAX_DURATION_SECONDS = 300

#: Hard cap on downloaded audio. Telegram's own getFile tops out around 20 MB;
#: this guards the decoder against anything unexpected reaching it.
MAX_AUDIO_BYTES = 20 * 1024 * 1024

#: STT sample rate every provider in novacode_cli.audio expects.
_SAMPLE_RATE = 16000


class VoiceNotesUnavailable(RuntimeError):
    """Voice transcription isn't installed. The message is shown to the user."""


class VoiceNoteTooLong(ValueError):
    """The clip exceeds :data:`MAX_DURATION_SECONDS`."""


def _decode_to_pcm16(data: bytes) -> np.ndarray:
    """Decode compressed audio to the 16 kHz mono int16 PCM the STT layer wants.

    ``faster_whisper.audio.decode_audio`` is PyAV-backed and handles OGG/Opus,
    MP3, M4A and WAV alike, resampling and downmixing to mono for us. Using it
    keeps this free of an ffmpeg subprocess, which would be one more thing to
    install and to fail on Windows.
    """
    try:
        import numpy as np
        from faster_whisper.audio import decode_audio
    except ImportError as exc:  # pragma: no cover - depends on install extras
        from novacode_cli.install_commands import install_command

        command = install_command(["faster-whisper"], "voice")
        raise VoiceNotesUnavailable(
            "Voice notes need Nova's voice packages (faster-whisper). On the computer "
            f"running Nova, install them with:\n    {command}\nthen restart Nova."
        ) from exc

    audio = decode_audio(io.BytesIO(data), sampling_rate=_SAMPLE_RATE)
    # decode_audio yields float32 in [-1, 1]; the STT Protocol takes int16.
    # Clip first: a hot recording can exceed 1.0 and would wrap to loud noise.
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)


def _load_stt() -> Any:
    """Build the STT backend from the saved ``/voice`` settings."""
    from novacode_cli.audio.pipeline import stt_from_voice_config
    from novacode_cli.config.nova_config import NovaConfig

    return stt_from_voice_config(NovaConfig().get_voice_config())


async def transcribe_voice_note(
    data: bytes, *, duration: float | None = None, stt: Any = None
) -> str:
    """Transcribe voice-note audio to text.

    Args:
        data: The raw audio file bytes, in any container PyAV can read.
        duration: Clip length in seconds when the platform reports one, so an
            over-long note is rejected before it is decoded.
        stt: An STT backend to use instead of the configured one (for tests).

    Returns:
        The transcript, or ``""`` when the clip held no recognizable speech.

    Raises:
        VoiceNoteTooLong: The clip is longer than :data:`MAX_DURATION_SECONDS`.
        VoiceNotesUnavailable: The voice extra isn't installed.
    """
    if duration is not None and duration > MAX_DURATION_SECONDS:
        msg = (
            f"That voice note is {int(duration)}s; the limit is "
            f"{MAX_DURATION_SECONDS}s. Send a shorter one, or type the message."
        )
        raise VoiceNoteTooLong(msg)
    if not data:
        return ""
    if len(data) > MAX_AUDIO_BYTES:
        msg = f"That audio is {len(data) // (1024 * 1024)} MB; the limit is 20 MB."
        raise VoiceNoteTooLong(msg)

    pcm = _decode_to_pcm16(data)
    if pcm.size == 0:
        return ""
    backend = stt if stt is not None else _load_stt()
    text = await backend.transcribe(pcm)
    return (text or "").strip()
