"""Voice provider registry — discoverable STT and TTS backends.

Every provider is declared in :data:`STT_PROVIDERS` or :data:`TTS_PROVIDERS` so
the ``/voice settings`` command can enumerate choices without importing anything.
Actual implementation classes are loaded lazily when the provider is selected.

The :class:`VoiceSTT` and :class:`VoiceTTS` Protocols define the interface each
backend must satisfy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np

# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceSTT(Protocol):
    """Speech-to-text. Transcribes raw 16 kHz mono int16 audio to a string."""

    async def transcribe(self, pcm_int16: np.ndarray) -> str:
        """Transcribe a spoken utterance.

        Args:
            pcm_int16: 16 kHz mono int16 numpy array of the utterance.

        Returns:
            The transcribed text, or ``""`` on silence / failure.
        """
        ...


@runtime_checkable
class VoiceTTS(Protocol):
    """Text-to-speech. Synthesises text and plays audio through speakers."""

    async def speak(self, text: str) -> None:
        """Speak ``text`` aloud. No-op for empty / whitespace-only text."""
        ...


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

STT_PROVIDERS: dict[str, dict[str, Any]] = {
    "faster-whisper": {
        "name": "Faster-Whisper",
        "description": "Local, open-source (CUDA/CPU)",
        "local": True,
        "requires_key": False,
        "default_model": "base",
        "option_field": "model",
        # Whisper's published size names. The list is deliberately short — the
        # picker's free-text field reaches any other build, including ones this
        # table does not know about.
        "options": [
            "base",
            "small",
            "medium",
            "large-v3",
            "distil-large-v3",
            "distil-medium.en",
            "distil-small.en",
        ],
        "option_labels": {
            "base": "base (fastest)",
            "large-v3": "large-v3 (most accurate)",
        },
    },
    "deepgram": {
        "name": "Deepgram",
        "description": "Cloud API — high-accuracy STT",
        "local": False,
        "requires_key": True,
        "default_model": "nova-2",
        "option_field": "model",
        # Deepgram's documented model names; `nova-2` is this repo's default.
        "options": ["nova-3", "nova-2", "enhanced", "base"],
    },
    "parakeet": {
        "name": "Parakeet (NVIDIA)",
        "description": "NVIDIA Parakeet-TDT-0.6B-v2 via sherpa-onnx (CPU/CUDA)",
        "local": True,
        "requires_key": False,
        "default_model": "parakeet-tdt-0.6b-v2",
        "option_field": "model",
        "options": ["parakeet-tdt-0.6b-v2"],
    },
}

TTS_PROVIDERS: dict[str, dict[str, Any]] = {
    "piper": {
        "name": "Piper",
        "description": "Local, low-latency TTS (CPU)",
        "local": True,
        "requires_key": False,
        "default_voice": "en_US-lessac-medium",
        "option_field": "voice",
        # Only voices this repo already references (the pipeline default and the
        # ones saved in Nova.config.json): Piper publishes hundreds, and listing
        # a name the local install has no model for would fail at synthesis.
        # Anything else goes in the picker's free-text field.
        "options": ["en_US-lessac-medium", "en_US-amy-medium", "en_US-libritts_r-medium"],
        "option_labels": {
            "en_US-lessac-medium": "en_US-lessac-medium (default)",
        },
    },
    "elevenlabs": {
        "name": "ElevenLabs",
        "description": "Cloud API — premium voice quality",
        "local": False,
        "requires_key": True,
        "default_voice": "21m00Tcm4TlvDq8ikWAM",
        "option_field": "voice_id",
        # Rows are voice IDs, not names. Only the repo's default is listed:
        # enumerating an account's other voices needs a network call with the
        # key, and a wrong ID fails at synthesis. Free-text covers the rest.
        "options": ["21m00Tcm4TlvDq8ikWAM"],
        "option_labels": {"21m00Tcm4TlvDq8ikWAM": "21m00Tcm4TlvDq8ikWAM (default)"},
    },
    "orpheus": {
        "name": "Orpheus (local, natural)",
        "description": "LLM-based, very natural — heavy (~2GB, slow on CPU)",
        "local": True,
        "requires_key": False,
        "default_voice": "tara",
        "option_field": "voice",
        # `tara` is this repo's default; `jess` is the other voice already in use
        # on this machine. Orpheus ships more — free-text reaches them.
        "options": ["tara", "jess"],
    },
    "pocket": {
        "name": "Pocket-TTS (Kyutai)",
        "description": "Local, CPU-optimized with voice cloning (CPU)",
        "local": True,
        "requires_key": False,
        "default_voice": "alba",
        "option_field": "voice",
        # As above: the repo default plus the voice already configured locally.
        "options": ["alba", "vera"],
    },
    "none": {
        "name": "Off",
        "description": "Silence — no spoken output",
        "local": True,
        "requires_key": False,
        "default_voice": "",
        "option_field": "voice",
        "options": [],
    },
}


def stt_provider_names() -> list[str]:
    """Sorted list of STT provider keys."""
    return sorted(STT_PROVIDERS)


def tts_provider_names() -> list[str]:
    """Sorted list of TTS provider keys."""
    return sorted(TTS_PROVIDERS)


# ---------------------------------------------------------------------------
# Null provider (TTS off)
# ---------------------------------------------------------------------------


class _NullTTS:
    """No-op TTS provider for when speech output is disabled."""

    async def speak(self, text: str) -> None:
        """Silently drop the text — no synthesis or playback."""
