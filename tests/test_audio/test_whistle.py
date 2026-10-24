"""Whistle STT adapter contracts without model downloads or native binaries."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from novacode_cli.audio.pipeline import build_stt
from novacode_cli.audio.providers import STT_PROVIDERS


def test_whistle_is_discoverable():
    meta = STT_PROVIDERS["whistle"]
    assert meta["options"] == ["whistle"]
    assert meta["local"]
    assert not meta["requires_key"]


def test_missing_whistle_dependency_has_install_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "needle", None)
    with pytest.raises(ImportError, match="cactus-needle"):
        build_stt("whistle")


async def test_whistle_normalizes_pcm_off_loop_and_loads_lazily(monkeypatch: pytest.MonkeyPatch):
    builds, samples, threads = [], [], []
    owner = threading.get_ident()

    class FakeWhistle:
        def __init__(self) -> None:
            builds.append(True)

        def transcribe(self, audio: np.ndarray, *, language: str | None) -> dict[str, str]:
            threads.append(threading.get_ident())
            samples.append((audio.copy(), language))
            return {"text": " hello Nova "}

    monkeypatch.setitem(sys.modules, "needle", SimpleNamespace(Whistle=FakeWhistle))
    stt = build_stt("whistle", {"whistle": {"model": "whistle", "language": "de"}})
    assert builds == []
    assert await stt.transcribe(np.array([], dtype=np.int16)) == ""
    assert builds == []
    pcm = np.array([-32768, 0, 32767], dtype=np.int16)
    assert await stt.transcribe(pcm) == "hello Nova"
    assert await stt.transcribe(pcm) == "hello Nova"
    assert builds == [True]
    assert all(ident != owner for ident in threads)
    assert samples[0][0].dtype == np.float32
    np.testing.assert_array_equal(samples[0][0], pcm.astype(np.float32) / 32768.0)
    assert samples[0][1] == "de"


async def test_whistle_calls_are_serialized_across_instances(monkeypatch: pytest.MonkeyPatch):
    active = peak = 0

    class FakeWhistle:
        def transcribe(self, audio: np.ndarray, *, language: str | None) -> dict[str, str]:
            nonlocal active, peak
            assert audio.ndim == 1
            assert language == "en"
            active += 1
            peak = max(peak, active)
            time.sleep(0.03)
            active -= 1
            return {"text": "ok"}

    monkeypatch.setitem(sys.modules, "needle", SimpleNamespace(Whistle=FakeWhistle))
    providers = [build_stt("whistle") for _ in range(4)]
    results = await asyncio.gather(*(p.transcribe(np.zeros(8, dtype=np.int16)) for p in providers))
    assert results == ["ok"] * 4
    assert peak == 1
    assert active == 0


async def test_whistle_runtime_failure_is_reported(monkeypatch: pytest.MonkeyPatch):
    class Broken:
        def __init__(self) -> None:
            msg = "native DLL unavailable"
            raise OSError(msg)

    monkeypatch.setitem(sys.modules, "needle", SimpleNamespace(Whistle=Broken))
    with pytest.raises(OSError, match="native DLL unavailable"):
        await build_stt("whistle").transcribe(np.zeros(8, dtype=np.int16))


@pytest.mark.parametrize(("key", "value"), [("model", "not-whistle"), ("language", "unknown")])
def test_whistle_rejects_unsupported_config(key: str, value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "needle", SimpleNamespace())
    with pytest.raises(ValueError, match="Whistle"):
        build_stt("whistle", {"whistle": {key: value}})


@pytest.mark.parametrize(("cached", "needs"), [(None, True), ("/cache/whistle.cact", False)])
def test_whistle_download_hint_only_probes_cache(
    cached: str | None, *, needs: bool, monkeypatch: pytest.MonkeyPatch
):
    def lookup(repo: str, filename: str) -> str | None:
        assert repo == "Cactus-Compute/whistle"
        assert filename == "whistle.cact"
        return cached

    monkeypatch.setitem(sys.modules, "needle", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(try_to_load_from_cache=lookup)
    )
    assert build_stt("whistle").needs_download is needs
