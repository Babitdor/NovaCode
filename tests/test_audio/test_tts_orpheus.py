"""Tests for the Orpheus TTS provider (hermetic — orpheus-cpp not installed).

Exercises the provider's shape, the synth→play path with a fake OrpheusCpp model,
the pipeline factory branch, capability detection, and the /voice install hint.
"""

from __future__ import annotations

import numpy as np
import pytest

from novacode_cli import audio
from novacode_cli.audio.tts_orpheus import OrpheusSpeaker


class FakeOrpheus:
    """Stand-in for orpheus_cpp.OrpheusCpp."""

    def __init__(self, *, verbose: bool = False, lang: str = "en") -> None:
        self.lang = lang
        self.spoken: list[tuple[str, str]] = []

    def stream_tts_sync(self, text, options):
        self.spoken.append((text, options.get("voice_id", "")))
        # Two int16 chunks at 24kHz, like the real streaming API.
        yield (24_000, np.array([1, 2, 3], dtype="int16"))
        yield (24_000, np.array([4, 5], dtype="int16"))


class TestOrpheusSpeaker:
    async def test_empty_text_is_noop(self):
        spk = OrpheusSpeaker()
        spk._model = FakeOrpheus()  # ensure no load attempt
        await spk.speak("   ")
        assert spk._model.spoken == []

    async def test_speak_streams_and_plays(self, monkeypatch):
        spk = OrpheusSpeaker(voice="leo")
        spk._model = FakeOrpheus()
        played: dict = {}

        def _fake_play(self, pcm):
            played["pcm"] = pcm

        monkeypatch.setattr(OrpheusSpeaker, "_play_and_wait", _fake_play)
        await spk.speak("hello there")
        # The two streamed chunks are concatenated and played.
        assert list(played["pcm"]) == [1, 2, 3, 4, 5]
        # The configured voice is passed through.
        assert spk._model.spoken == [("hello there", "leo")]

    async def test_no_chunks_skips_playback(self, monkeypatch):
        class EmptyOrpheus(FakeOrpheus):
            def stream_tts_sync(self, text, options):
                return iter(())

        spk = OrpheusSpeaker()
        spk._model = EmptyOrpheus()
        called = {"n": 0}
        monkeypatch.setattr(
            OrpheusSpeaker, "_play_and_wait", lambda self, pcm: called.__setitem__("n", 1)
        )
        await spk.speak("nothing comes back")
        assert called["n"] == 0

    def test_ensure_voice_builds_model_once(self, monkeypatch):
        import novacode_cli.audio.tts_orpheus as mod

        builds = {"n": 0}

        class _Mod:
            def __init__(self, _lang: str):
                builds["n"] += 1

        monkeypatch.setattr(mod, "_load_model", _Mod)
        spk = mod.OrpheusSpeaker()
        spk._ensure_voice()
        spk._ensure_voice()  # idempotent
        assert builds["n"] == 1

    def test_illegal_instruction_becomes_actionable_error(self, monkeypatch):
        import novacode_cli.audio.tts_orpheus as mod

        def _boom(_lang: str):
            err = OSError("Windows Error 0xc000001d")
            err.winerror = -1073741795
            raise err

        monkeypatch.setattr(mod, "_load_model", _boom)
        spk = mod.OrpheusSpeaker()
        with pytest.raises(RuntimeError, match="instruction set"):
            spk._ensure_voice()

    def test_other_oserror_propagates(self, monkeypatch):
        import novacode_cli.audio.tts_orpheus as mod

        def _boom(_lang: str):
            raise OSError("disk full")

        monkeypatch.setattr(mod, "_load_model", _boom)
        spk = mod.OrpheusSpeaker()
        with pytest.raises(OSError, match="disk full"):
            spk._ensure_voice()


class TestCapability:
    def test_is_orpheus_available_matches_import(self):
        # The flag must reflect whether orpheus_cpp actually imports — not a
        # hardcoded value (it may or may not be installed in a given env).
        import importlib.util

        present = importlib.util.find_spec("orpheus_cpp") is not None
        assert audio.is_orpheus_available() is present

    def test_install_hint_mentions_packages(self):
        hint = audio.orpheus_install_hint()
        assert "orpheus-cpp" in hint
        assert "llama-cpp-python" in hint


class TestPipelineFactory:
    def test_build_tts_returns_orpheus(self):
        from novacode_cli.audio.pipeline import VoicePipeline

        pipeline = VoicePipeline(
            tts_provider="orpheus",
            provider_configs={"orpheus": {"voice": "zoe", "lang": "en"}},
        )
        tts = pipeline._build_tts()
        assert isinstance(tts, OrpheusSpeaker)
        assert tts._voice == "zoe"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
