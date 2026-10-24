"""CPU-only Kokoro 82M INT8 speech using the optional kokoro-onnx runtime."""

from __future__ import annotations

from typing import Any

from novacode_cli.audio.downloads import download_file
from novacode_cli.audio.playback import play_and_wait, run_speech
from novacode_cli.audio.speakable import speech_sentences
from novacode_cli.config.config import HOME_DIR

_VOICE_DIR = HOME_DIR / "voice" / "kokoro"
_ASSET_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_MODEL_FILE = "kokoro-v1.0.int8.onnx"
_VOICES_FILE = "voices-v1.0.bin"


class KokoroSpeaker:
    """Keep one quantized CPU model loaded and play speech sentence by sentence."""

    def __init__(self, *, voice: str = "af_heart") -> None:
        """Select an English voice; defer imports and downloads until first use."""
        self._voice_name = voice
        self._model: Any = None

    @property
    def needs_download(self) -> bool:
        """Whether either required runtime asset is missing."""
        return any(not (_VOICE_DIR / name).is_file() for name in (_MODEL_FILE, _VOICES_FILE))

    def _ensure_voice(self) -> None:
        if self._model is not None:
            return
        try:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            from novacode_cli.audio import kokoro_install_hint

            raise ImportError(kokoro_install_hint()) from exc

        for name in (_MODEL_FILE, _VOICES_FILE):
            target = _VOICE_DIR / name
            if not target.is_file():
                download_file(f"{_ASSET_URL}/{name}", target)
        options = ort.SessionOptions()
        # Leave CPU capacity for Nova and avoid oversubscription on small machines.
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(_VOICE_DIR / _MODEL_FILE), options, providers=["CPUExecutionProvider"]
        )
        model = Kokoro.from_session(session, str(_VOICE_DIR / _VOICES_FILE))
        if self._voice_name not in model.get_voices():
            msg = f"Unknown Kokoro voice: {self._voice_name}"
            raise ValueError(msg)
        if not self._voice_name.startswith(("af_", "am_", "bf_", "bm_")):
            msg = "Nova's Kokoro provider currently supports English voices only."
            raise ValueError(msg)
        self._model = model

    async def speak(self, text: str) -> None:
        """Synthesize off-loop with shared playback ownership and cancellation."""
        if text.strip():
            await run_speech(lambda: self._speak_sync(text))

    def _speak_sync(self, text: str) -> None:
        self._ensure_voice()
        language = "en-gb" if self._voice_name.startswith("b") else "en-us"
        for sentence in speech_sentences(text):
            pcm, sample_rate = self._model.create(sentence, voice=self._voice_name, lang=language)
            if not play_and_wait(pcm, sample_rate):
                return
