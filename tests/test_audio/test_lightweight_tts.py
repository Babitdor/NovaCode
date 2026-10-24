"""Lightweight providers load lazily, use CPU, and share safe speech playback."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest

from novacode_cli.audio import tts_kokoro, tts_sano
from novacode_cli.audio.pipeline import VoicePipeline

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Real provider wrappers, fake runtimes/downloads/output; no model or device."""
    calls: list[object] = []
    downloads: list[str] = []
    plays: list[int] = []
    workers: list[int] = []

    def download(url: str, target: Path) -> None:
        downloads.append(url)
        workers.append(threading.get_ident())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake model")

    def session(path: str, options: SimpleNamespace, **kwargs: object) -> object:
        calls.append((path, options, kwargs))
        return object()

    class Kokoro:
        @staticmethod
        def from_session(_session: object, _voices: str) -> Kokoro:
            return Kokoro()

        def get_voices(self) -> list[str]:
            return ["af_heart", "bf_emma", "jf_alpha"]

        def create(self, text: str, *, voice: str, lang: str) -> tuple[np.ndarray, int]:
            calls.append((text, voice, lang))
            return np.ones(8, dtype=np.float32), 24_000

    class Synthesizer:
        def __init__(self, *, voice: str, voice_dir: Path) -> None:
            calls.append((voice, voice_dir))

        def synthesize(self, text: str) -> SimpleNamespace:
            calls.append(text)
            return SimpleNamespace(audio=np.ones(8, dtype=np.float32), sample_rate=22_050)

    def play(_pcm: np.ndarray, rate: int) -> bool:
        plays.append(rate)
        return True

    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=Kokoro))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=session),
    )
    monkeypatch.setitem(sys.modules, "sanotts", SimpleNamespace(Synthesizer=Synthesizer))
    for module in (tts_kokoro, tts_sano):
        monkeypatch.setattr(module, "_VOICE_DIR", tmp_path / module.__name__)
        monkeypatch.setattr(module, "download_file", download)
        monkeypatch.setattr(module, "play_and_wait", play)
    return SimpleNamespace(calls=calls, downloads=downloads, plays=plays, workers=workers)


@pytest.mark.parametrize(
    ("provider", "rate", "files"), [("kokoro", 24_000, 2), ("sano", 22_050, 3)]
)
async def test_pipeline_reuses_assets_and_runtime_off_loop(
    provider: str, rate: int, files: int, engines: SimpleNamespace
):
    pipeline = VoicePipeline(tts_provider=provider)
    main_thread = threading.get_ident()
    assert pipeline.tts_needs_download
    assert engines.downloads == engines.calls == []
    await pipeline.speak("First sentence. Second sentence.")
    await pipeline.speak("Next reply.")
    assert not pipeline.tts_needs_download
    assert len(engines.downloads) == files
    assert set(engines.workers) != {main_thread}
    assert main_thread not in engines.workers
    assert engines.plays == [rate, rate, rate]
    if provider == "kokoro":
        sessions = [
            call
            for call in engines.calls
            if isinstance(call, tuple) and len(call) == 3 and isinstance(call[1], SimpleNamespace)
        ]
        assert len(sessions) == 1
        assert sessions[0][1].intra_op_num_threads == 2
        assert sessions[0][2]["providers"] == ["CPUExecutionProvider"]
    else:
        assert len([call for call in engines.calls if isinstance(call, tuple)]) == 1


@pytest.mark.parametrize("provider", ["kokoro", "sano"])
async def test_cancelled_output_does_not_generate_later_sentences(
    provider: str, engines: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    module = tts_kokoro if provider == "kokoro" else tts_sano
    monkeypatch.setattr(module, "play_and_wait", lambda *_args: False)
    await VoicePipeline(tts_provider=provider).speak("First. NEVER GENERATE.")
    assert "NEVER GENERATE" not in str(engines.calls)


@pytest.mark.parametrize(("provider", "module"), [("kokoro", "kokoro_onnx"), ("sano", "sanotts")])
async def test_missing_optional_runtime_has_install_hint_and_empty_speech_is_quiet(
    provider: str, module: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(sys.modules, module, None)
    pipeline = VoicePipeline(tts_provider=provider)
    await pipeline.speak("   ")
    with pytest.raises(
        ImportError,
        match=f"voice-{provider}|{'kokoro-onnx' if provider == 'kokoro' else 'sanotts'}",
    ):
        await pipeline.speak("Hello.")


async def test_configured_british_voice_selects_british_phonemizer(engines: SimpleNamespace):
    pipeline = VoicePipeline(
        tts_provider="kokoro", provider_configs={"kokoro": {"voice": "bf_emma"}}
    )
    await pipeline.speak("Hello.")
    assert ("Hello.", "bf_emma", "en-gb") in engines.calls


@pytest.mark.parametrize("voice", ["unknown", "jf_alpha"])
async def test_kokoro_rejects_unknown_or_unsupported_language(voice: str, engines: SimpleNamespace):
    with pytest.raises(ValueError, match=r"Unknown Kokoro voice|English voices only"):
        await tts_kokoro.KokoroSpeaker(voice=voice).speak("Hello.")
    assert engines.plays == []


def test_sano_rejects_voice_paths_before_downloading():
    with pytest.raises(ValueError, match="Unknown sanoTTS voice"):
        tts_sano.SanoSpeaker(voice="../../elsewhere")


@pytest.mark.parametrize(
    ("module", "cls"), [(tts_kokoro, tts_kokoro.KokoroSpeaker), (tts_sano, tts_sano.SanoSpeaker)]
)
@pytest.mark.usefixtures("engines")
def test_partial_cache_is_not_ready(module: ModuleType, cls: type):
    speaker = cls()
    speaker._ensure_voice()
    assert not speaker.needs_download
    # A leftover marker cannot make an incomplete multi-file model look ready.
    next(module._VOICE_DIR.rglob("*.bin")).unlink()
    assert speaker.needs_download
