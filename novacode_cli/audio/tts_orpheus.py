"""Orpheus local TTS — LLM-based, very natural voices, via orpheus-cpp (CPU).

Orpheus is a 3B-param Llama-based TTS. The standard path uses vLLM + CUDA, which
isn't available on Windows / CPU-only machines, so this wrapper uses the
``orpheus-cpp`` (llama.cpp / GGUF) backend — fully local, no GPU, no API key.

Trade-off: it's heavy. The GGUF model is ~2GB (downloaded on first use) and
synthesis takes multiple seconds per reply on CPU. It's an **opt-in** provider;
Piper stays the low-latency default. Selected via ``/voice settings tts orpheus``.

The module imports cleanly without ``orpheus-cpp`` installed — heavy imports are
deferred into the methods, and :func:`novacode_cli.audio.is_orpheus_available`
gates selection with an install hint.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from novacode_cli.audio.playback import play_and_wait, run_speech

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("nova.audio.tts_orpheus")

#: Orpheus outputs 24 kHz mono int16 audio.
_SAMPLE_RATE = 24_000
_DEFAULT_VOICE = "tara"
_DEFAULT_LANG = "en"
#: English voices shipped with the finetune-prod model.
VOICES = ("tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe")

#: Windows NTSTATUS for STATUS_ILLEGAL_INSTRUCTION (0xC000001D), seen as a
#: negative winerror; the CPU lacks an instruction the native wheel uses.
_WIN_ILLEGAL_INSTRUCTION = -1073741795


def _is_illegal_instruction(exc: OSError) -> bool:
    """Whether ``exc`` is the AVX/illegal-instruction crash from a bad wheel."""
    winerror = getattr(exc, "winerror", None)
    if winerror == _WIN_ILLEGAL_INSTRUCTION:
        return True
    text = str(exc).lower()
    return "0xc000001d" in text or "illegal instruction" in text


class OrpheusSpeaker:
    """Synthesises text with Orpheus (orpheus-cpp) and plays via sounddevice."""

    def __init__(self, *, voice: str = _DEFAULT_VOICE, lang: str = _DEFAULT_LANG) -> None:
        """Configure voice + language; the model loads/downloads on first use."""
        self._voice = voice or _DEFAULT_VOICE
        self._lang = lang or _DEFAULT_LANG
        self._model: Any = None

    def _ensure_voice(self) -> None:
        """Build the OrpheusCpp model once (named for warmup compatibility).

        ``VoicePipeline.warmup`` eagerly calls ``_ensure_voice`` on the TTS
        provider, so this name lets Orpheus pre-load at startup like Piper.

        Translates the common ``0xc000001d`` (illegal-instruction) crash from a
        prebuilt ``llama-cpp-python`` wheel into an actionable message — it means
        the CPU lacks the AVX2/AVX-512 instructions the wheel was built with.
        """
        if self._model is None:
            from orpheus_cpp import OrpheusCpp

            logger.info("Loading Orpheus (orpheus-cpp) — this may download ~2GB")
            try:
                self._model = OrpheusCpp(verbose=False, lang=self._lang)
            except OSError as exc:
                if _is_illegal_instruction(exc):
                    from novacode_cli.install_commands import install_command

                    rebuild = install_command(
                        ["llama-cpp-python"], args="--force-reinstall --no-binary llama-cpp-python"
                    )
                    msg = (
                        "Orpheus failed to load: this CPU doesn't support the "
                        "instruction set (AVX2/AVX-512) the prebuilt "
                        "llama-cpp-python wheel was built with. Rebuild it for "
                        f"your CPU:\n  {rebuild}\n"
                        "(needs a C++ compiler), or switch back with "
                        "/voice settings tts piper."
                    )
                    raise RuntimeError(msg) from exc
                raise

    async def speak(self, text: str) -> None:
        """Synthesise ``text`` and play it (off the event loop). No-op if empty."""
        if not text.strip():
            return
        await run_speech(lambda: self._speak_sync(text))

    def _speak_sync(self, text: str) -> None:
        import numpy as np

        self._ensure_voice()
        chunks: list[np.ndarray] = []
        for _sr, chunk in self._model.stream_tts_sync(text, options={"voice_id": self._voice}):
            arr = np.asarray(chunk).reshape(-1).astype("int16")
            if len(arr) > 0:
                chunks.append(arr)
        if not chunks:
            return
        self._play_and_wait(np.concatenate(chunks))

    def _play_and_wait(self, pcm: np.ndarray) -> None:
        play_and_wait(pcm, _SAMPLE_RATE)
