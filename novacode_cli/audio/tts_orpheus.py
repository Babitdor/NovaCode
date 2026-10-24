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

from novacode_cli.audio.downloads import report_model_loading
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


def _model_asset(lang: str) -> tuple[str, str]:
    from orpheus_cpp import OrpheusCpp

    repo = OrpheusCpp.lang_to_model[lang]
    return repo, repo.rsplit("/", 1)[-1].lower().replace("-gguf", ".gguf")


def _load_model(lang: str) -> Any:  # noqa: ANN401 -- optional native runtime.
    """Load orpheus-cpp 0.0.3 with a bounded context and CPU thread pools.

    Upstream's constructor hardcodes n_ctx=0 (the full trained context), which
    can allocate many GB of KV cache immediately after download. It exposes no
    context parameter. Keep its streaming/decoding methods and initialize the
    two runtime fields they use explicitly; the optional dependency is pinned.
    """
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from llama_cpp import Llama
    from orpheus_cpp import OrpheusCpp

    repo, filename = _model_asset(lang)
    path = hf_hub_download(repo_id=repo, filename=filename)
    report_model_loading()
    llm = Llama(
        model_path=path,
        n_ctx=4096,
        n_batch=128,
        n_threads=2,
        n_threads_batch=2,
        n_gpu_layers=0,
        verbose=False,
    )
    decoder_path = hf_hub_download(
        repo_id="onnx-community/snac_24khz-ONNX",
        subfolder="onnx",
        filename="decoder_model.onnx",
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    report_model_loading()
    decoder = ort.InferenceSession(
        decoder_path, sess_options=options, providers=["CPUExecutionProvider"]
    )
    model = OrpheusCpp.__new__(OrpheusCpp)
    model._llm = llm
    model._snac_session = decoder
    return model


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

    @property
    def needs_download(self) -> bool:
        """Check both GGUF and codec assets without constructing a native model."""
        if self._model is not None:
            return False
        try:
            from huggingface_hub import try_to_load_from_cache

            repo, filename = _model_asset(self._lang)
            return not all(
                isinstance(try_to_load_from_cache(repo_id, asset), str)
                for repo_id, asset in (
                    (repo, filename),
                    ("onnx-community/snac_24khz-ONNX", "onnx/decoder_model.onnx"),
                )
            )
        except ImportError:
            return True

    def _ensure_voice(self) -> None:
        """Build the OrpheusCpp model once (named for warmup compatibility).

        ``VoicePipeline.warmup`` eagerly calls ``_ensure_voice`` on the TTS
        provider, so this name lets Orpheus pre-load at startup like Piper.

        Translates the common ``0xc000001d`` (illegal-instruction) crash from a
        prebuilt ``llama-cpp-python`` wheel into an actionable message — it means
        the CPU lacks the AVX2/AVX-512 instructions the wheel was built with.
        """
        if self._model is None:
            logger.info("Loading Orpheus (orpheus-cpp) — this may download ~2GB")
            try:
                self._model = _load_model(self._lang)
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
