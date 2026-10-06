"""VoicePipeline — orchestrates capture → VAD → STT, and TTS playback.

UI-agnostic: the TUI constructs one of these and drives it via callbacks, so the
audio logic stays testable and reusable. All heavy work runs off the event loop
(``asyncio.to_thread``). The ``tts_active`` flag lets the always-listening loop
**pause while Nova is speaking**, so it never transcribes its own output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from novacode_cli.audio.providers import VoiceSTT, VoiceTTS

logger = logging.getLogger("nova.audio.pipeline")

_TTS_POLL_S = 0.1

_ACTIVE_PIPELINE: VoicePipeline | None = None


def get_active_pipeline() -> VoicePipeline | None:
    """Return the currently active VoicePipeline instance."""
    return _ACTIVE_PIPELINE


def _stored_or_config_key(cfg: dict[str, Any], env_var: str) -> str:
    """Resolve a cloud voice provider's key, preferring the credential store.

    The config field is checked first only because it is the older location and
    may still hold a key on a machine where the startup migration has not run;
    `credential_value` then covers the keychain (which is where `/auth` and
    `/voice settings --key` now write) and the environment.

    A key that no longer resolves returns ``""`` so the backend still reports its
    own "no API key" error rather than the pipeline guessing.

    Args:
        cfg: The provider's config block.
        env_var: Credential env var backing this provider.

    Returns:
        The API key, or an empty string when none is configured.
    """
    from novacode_cli.config.credentials import credential_value

    return cfg.get("api_key") or cfg.get("key") or credential_value(env_var)


def build_stt(provider: str, provider_configs: dict[str, Any] | None = None) -> VoiceSTT:
    """Create an STT backend by provider name.

    Module-level rather than a :class:`VoicePipeline` method because callers
    that only transcribe — a remote voice note, say — must not construct a
    pipeline: doing so installs itself as the process-wide ``_ACTIVE_PIPELINE``
    and would hijack the live voice session.
    """
    cfg = (provider_configs or {}).get(provider, {})
    if provider == "faster-whisper":
        from novacode_cli.audio.stt import Transcriber

        return Transcriber(
            model_size=cfg.get("model", "distil-large-v3"),
            device=cfg.get("device", "auto"),
            language=cfg.get("language", "en"),
        )
    if provider == "deepgram":
        from novacode_cli.audio.stt_deepgram import DeepgramTranscriber

        return DeepgramTranscriber(
            api_key=_stored_or_config_key(cfg, "DEEPGRAM_API_KEY"),
            model=cfg.get("model", "nova-2"),
        )
    if provider == "parakeet":
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "The 'sherpa-onnx' package is required for Parakeet STT.\n"
                "If developer/local install, run: pip install sherpa-onnx\n"
                "If global uv tool install, run: uv tool install --with sherpa-onnx novacode-cli --reinstall"
            ) from e

        from novacode_cli.audio.stt_parakeet import ParakeetTranscriber

        return ParakeetTranscriber(
            num_threads=cfg.get("threads", 2),
        )
    msg = f"Unknown STT provider: {provider!r}"
    raise ValueError(msg)


def stt_from_voice_config(cfg: dict[str, Any]) -> VoiceSTT:
    """Build the STT backend described by a saved ``/voice`` config block.

    Applies the same legacy-key merge :class:`VoicePipeline` does, so a voice
    note is transcribed by whatever provider and model the user selected rather
    than by a second, silently different default.
    """
    provider_configs = {k: dict(v) for k, v in (cfg.get("providers") or {}).items()}
    whisper = provider_configs.setdefault("faster-whisper", {})
    whisper.setdefault("model", cfg.get("stt_model", "base"))
    whisper.setdefault("device", cfg.get("stt_device", "auto"))
    return build_stt(cfg.get("stt_provider", "faster-whisper"), provider_configs)


class VoicePipeline:
    """High-level voice I/O: one-shot capture, continuous listen, and speak."""

    def __init__(
        self,
        *,
        stt_provider: str = "faster-whisper",
        tts_provider: str = "piper",
        provider_configs: dict[str, Any] | None = None,
        # Legacy flat params (kept for backward compat, merged into provider_configs).
        stt_model: str = "base",
        stt_device: str = "auto",
        tts_voice: str = "en_US-lessac-medium",
    ) -> None:
        """Record component config; nothing is loaded until first use."""
        global _ACTIVE_PIPELINE
        _ACTIVE_PIPELINE = self
        self._stt_provider = stt_provider
        self._tts_provider = tts_provider
        _pc = dict(provider_configs or {})
        _pc.setdefault("faster-whisper", {})
        _pc["faster-whisper"].setdefault("model", stt_model)
        _pc["faster-whisper"].setdefault("device", stt_device)
        _pc.setdefault("piper", {})
        _pc["piper"].setdefault("voice", tts_voice)
        self._provider_configs = _pc
        self._capture: Any = None
        self._vad: Any = None
        self._stt: Any = None
        self._tts: Any = None
        self._tts_active = False

    @property
    def tts_active(self) -> bool:
        """Whether TTS is currently playing (listen loop pauses while True)."""
        return self._tts_active

    def _ensure_components(self) -> None:
        self._ensure_input_components()
        self._ensure_tts()

    def _ensure_input_components(self) -> None:
        """Build microphone components only when capturing or preloading input."""
        from novacode_cli.audio.capture import AudioCapture
        from novacode_cli.audio.vad import SileroVad

        if self._capture is None:
            self._capture = AudioCapture()
        if self._vad is None:
            self._vad = SileroVad()
        if self._stt is None:
            self._stt = self._build_stt()

    def _ensure_tts(self) -> None:
        """Build the speech provider without allocating the input stack."""
        if self._tts is None:
            self._tts = self._build_tts()

    def _build_stt(self) -> VoiceSTT:
        """Create the STT provider selected by ``stt_provider``."""
        return build_stt(self._stt_provider, self._provider_configs)

    def _build_tts(self) -> VoiceTTS:
        """Create the TTS provider selected by ``tts_provider``."""
        provider = self._tts_provider
        cfg = self._provider_configs.get(provider, {})
        if provider == "piper":
            from novacode_cli.audio.tts import Speaker

            return Speaker(voice=cfg.get("voice", "en_US-lessac-medium"))
        if provider == "elevenlabs":
            from novacode_cli.audio.stt_elevenlabs import ElevenLabsSpeaker

            return ElevenLabsSpeaker(
                api_key=_stored_or_config_key(cfg, "ELEVENLABS_API_KEY"),
                voice_id=cfg.get("voice_id", "21m00Tcm4TlvDq8ikWAM"),
            )
        if provider == "orpheus":
            from novacode_cli.audio.tts_orpheus import OrpheusSpeaker

            return OrpheusSpeaker(
                voice=cfg.get("voice", "tara"),
                lang=cfg.get("lang", "en"),
            )
        if provider == "pocket":
            try:
                import pocket_tts  # noqa: F401
            except ImportError as e:
                raise ImportError(
                    "The 'pocket-tts' package is required for Pocket-TTS.\n"
                    "If developer/local install, run: pip install pocket-tts\n"
                    "If global uv tool install, run: uv tool install --with pocket-tts novacode-cli --reinstall"
                ) from e

            from novacode_cli.audio.tts_pocket import PocketSpeaker

            return PocketSpeaker(
                voice=cfg.get("voice", "alba"),
            )
        if provider == "none":
            from novacode_cli.audio.providers import _NullTTS

            return _NullTTS()
        msg = f"Unknown TTS provider: {provider!r}"
        raise ValueError(msg)

    async def warmup(self, *, input_audio: bool = True) -> None:
        """Pre-load VAD + STT + TTS models off the loop so first use isn't laggy.

        Only the local providers have models to pre-load (cloud providers have
        nothing to warm up). Best-effort: any load error is logged and ignored —
        the model simply loads on first real use instead.
        """
        if input_audio:
            self._ensure_components()
        else:
            self._ensure_tts()
        # (label, awaitable-or-None) — cloud providers expose no eager loader.
        _stt_load = getattr(self._stt, "_ensure_model", None)
        _tts_load = getattr(self._tts, "_ensure_voice", None)
        tasks: list[tuple[str, Any]] = []
        if input_audio:
            tasks.append(("VAD", self._vad.ensure_model_async()))
        if input_audio and _stt_load is not None:
            tasks.append(("STT", asyncio.to_thread(_stt_load)))
        if _tts_load is not None:
            tasks.append(("TTS", asyncio.to_thread(_tts_load)))
        for label, loader in tasks:
            try:
                await loader
            except Exception:
                logger.exception("Voice warmup failed for %s; will load on first use", label)

    async def capture_utterance(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> str | None:
        """Push-to-talk: record one utterance, return its transcript (or ``None``)."""
        self._ensure_input_components()
        self._capture.start()
        self._capture.drain()
        try:
            pcm = await self._vad.collect_utterance_async(
                self._capture.read, should_stop=should_stop
            )
        finally:
            self._capture.stop()  # PTT releases the mic between utterances
        if pcm is None or len(pcm) == 0:
            return None
        return (await self._stt.transcribe(pcm)) or None

    async def speak(self, text: str) -> None:
        """Speak ``text`` aloud, flagging ``tts_active`` for the duration."""
        if not text.strip():
            return
        self._ensure_tts()
        self._tts_active = True
        try:
            await self._tts.speak(text)
        finally:
            self._tts_active = False

    async def listen_loop(
        self,
        on_transcript: Callable[[str], Awaitable[None] | None],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Always-listening: capture utterances continuously, emit each transcript.

        Pauses capture while ``tts_active`` so Nova never hears itself.
        """
        self._ensure_input_components()
        self._capture.start()
        try:
            while True:
                if should_stop is not None and should_stop():
                    return
                if self._tts_active:
                    self._capture.drain()
                    await asyncio.sleep(_TTS_POLL_S)
                    continue

                pcm = await self._vad.collect_utterance_async(
                    self._capture.read,
                    should_stop=lambda: self._tts_active or bool(should_stop and should_stop()),
                )
                # Discard anything captured if TTS started or we were cancelled.
                if pcm is None or len(pcm) == 0 or self._tts_active:
                    continue
                text = await self._stt.transcribe(pcm)
                if not text:
                    continue
                result = on_transcript(text)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            self._capture.stop()

    def stop(self) -> None:
        """Release the microphone."""
        if self._capture is not None:
            self._capture.stop()

    @property
    def tts_needs_download(self) -> bool:
        """Whether the TTS provider needs to download files before speaking."""
        self._ensure_tts()
        return getattr(self._tts, "needs_download", False)

    @property
    def stt_needs_download(self) -> bool:
        """Whether the STT provider needs to download its model before use."""
        if self._stt is None:
            self._stt = self._build_stt()
        return getattr(self._stt, "needs_download", False)

    def downloads_pending(self) -> list[str]:
        """Return labels (``["STT", "TTS"]``) for models still needing download.

        Cheap: only stats the filesystem / HF cache via each provider's
        ``needs_download`` — no model is loaded. Empty when everything is cached.
        """
        self._ensure_components()
        pending: list[str] = []
        if getattr(self._stt, "needs_download", False):
            pending.append("STT")
        if getattr(self._tts, "needs_download", False):
            pending.append("TTS")
        return pending
