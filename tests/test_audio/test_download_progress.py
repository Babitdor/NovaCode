"""Voice progress uses actual transfers, without network or model downloads."""

from __future__ import annotations

import asyncio
import io
import sys
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from novacode_cli.audio import downloads
from novacode_cli.audio.downloads import (
    ModelDownload,
    download_file,
    download_progress,
    watch_downloads,
)
from novacode_cli.audio.pipeline import VoicePipeline
from tests.test_audio.test_pipeline_warmup import BoomSTT, FakeLocalTTS, FakeVad, _wire

if TYPE_CHECKING:
    from pathlib import Path


class Response(io.BytesIO):
    def __init__(self, body: bytes, length: str | None) -> None:
        super().__init__(body)
        self.headers = {"Content-Length": length} if length is not None else {}


async def test_direct_download_reports_bytes_and_replaces_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    events = []
    body = b"model" * 60_000

    class Slow(Response):
        def read(self, size: int = -1) -> bytes:
            time.sleep(0.05)
            return super().read(size)

    response = Slow(body, str(len(body)))
    monkeypatch.setattr(downloads.urllib.request, "urlopen", lambda *_a, **_kw: response)
    target = tmp_path / "voice.onnx"
    target.write_bytes(b"old model")
    async with watch_downloads("TTS", "piper", SimpleNamespace(needs_download=True), events.append):
        await asyncio.to_thread(download_file, "https://example.invalid/voice.onnx", target)
    assert target.read_bytes() == body
    assert events[0].state == "loading"
    assert events[-1].state == "ready"
    transfers = [e for e in events if e.state == "downloading"]
    assert any(0 < e.completed < len(body) for e in transfers)
    assert transfers[-1].completed == len(body)
    assert transfers[-1].total == len(body)
    assert transfers[-1].filename == "voice.onnx"
    assert list(tmp_path.glob("*.tmp")) == []  # noqa: ASYNC240 -- tiny local fixture.


@pytest.mark.parametrize("length", [None, "not-a-number"])
async def test_unknown_size_has_no_fabricated_total(
    length: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    events = []
    monkeypatch.setattr(
        downloads.urllib.request, "urlopen", lambda *_a, **_kw: Response(b"model", length)
    )
    async with watch_downloads(
        "STT", "parakeet", SimpleNamespace(needs_download=True), events.append
    ):
        await asyncio.to_thread(download_file, "https://example.invalid/model", tmp_path / "model")
    transfer = [e for e in events if e.state == "downloading"][-1]
    assert transfer.total is None
    assert transfer.completed == 5


async def test_truncated_download_keeps_previous_model_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    events = []
    monkeypatch.setattr(
        downloads.urllib.request, "urlopen", lambda *_a, **_kw: Response(b"short", "100")
    )
    target = tmp_path / "model.onnx"
    target.write_bytes(b"previous")
    with pytest.raises(OSError, match="Incomplete download"):
        async with watch_downloads(
            "STT", "parakeet", SimpleNamespace(needs_download=True), events.append
        ):
            await asyncio.to_thread(download_file, "https://example.invalid/model", target)
    assert target.read_bytes() == b"previous"
    assert events[-1].state == "error"
    assert not any(e.state == "ready" for e in events)
    assert list(tmp_path.glob("*.tmp")) == []  # noqa: ASYNC240 -- tiny local fixture.


async def test_hub_progress_is_context_local_and_preserves_nonvoice_calls(
    monkeypatch: pytest.MonkeyPatch,
):
    received = []

    def fetch(name: str, *, tqdm_class: type[Any] | None = None) -> str:
        received.append(tqdm_class)
        if tqdm_class is not None:
            # The bound class must work even from a worker without ContextVars.
            def transfer() -> None:
                with tqdm_class(total=100, initial=25, unit="B", desc=name) as bar:
                    bar.update(75)

            worker = threading.Thread(target=transfer)
            worker.start()
            worker.join()
        return name

    hub = SimpleNamespace(hf_hub_download=fetch, snapshot_download=fetch)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    first, second = [], []

    async def load(name: str, events: list[ModelDownload]) -> None:
        async with watch_downloads(
            "STT", name, SimpleNamespace(needs_download=True), events.append
        ):
            await asyncio.to_thread(hub.hf_hub_download, name)

    await asyncio.gather(load("first.bin", first), load("second.bin", second))
    assert {e.filename for e in first if e.state == "downloading"} == {"first.bin"}
    assert {e.filename for e in second if e.state == "downloading"} == {"second.bin"}
    assert first[-1].completed == second[-1].completed == 100
    assert first[-1].state == second[-1].state == "ready"
    assert received[0] is not received[1]
    assert hub.hf_hub_download("outside") == "outside"
    assert received[-1] is None


async def test_cached_load_is_quiet_and_old_hub_versions_still_load(
    monkeypatch: pytest.MonkeyPatch,
):
    calls, events = [], []

    def old_download(name: str) -> str:
        calls.append(name)
        return "cached"

    hub = SimpleNamespace(hf_hub_download=old_download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    async with watch_downloads(
        "STT", "whistle", SimpleNamespace(needs_download=False), events.append
    ):
        assert await asyncio.to_thread(hub.hf_hub_download, "whistle") == "cached"
    assert calls == ["whistle"]
    assert events == []


async def test_cancelled_scope_ignores_late_worker_updates(monkeypatch: pytest.MonkeyPatch):
    progress = []
    reporter_ready, release = threading.Event(), threading.Event()
    events = []

    def fetch(*, tqdm_class: type[Any] | None = None) -> None:
        assert tqdm_class is not None
        with tqdm_class(total=100, unit="B", desc="weights") as bar:
            progress.append(bar)
            reporter_ready.set()
            assert release.wait(2)
            bar.update(100)

    hub = SimpleNamespace(hf_hub_download=fetch)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    async def load() -> None:
        async with watch_downloads(
            "STT", "whistle", SimpleNamespace(needs_download=True), events.append
        ):
            await asyncio.to_thread(hub.hf_hub_download)

    task = asyncio.create_task(load())
    try:
        assert await asyncio.to_thread(reporter_ready.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        count = len(events)
        progress[0].update(1)
        assert len(events) == count
        assert events[-1].state == "cancelled"
    finally:
        release.set()


async def test_warmup_failure_is_not_reported_as_ready():
    events = []
    pipeline = VoicePipeline(on_download_progress=events.append)
    broken = BoomSTT()
    broken.needs_download = True
    tts = FakeLocalTTS()
    _wire(pipeline, vad=FakeVad(), stt=broken, tts=tts)
    assert await pipeline.warmup() is False
    assert events[-1].state == "error"
    assert events[-1].kind == "STT"
    assert tts.loaded  # Preserve the best-effort warmup of the other provider.


async def test_command_context_carries_progress_to_new_pipeline():
    events = []
    pipeline = VoicePipeline()
    broken = BoomSTT()
    broken.needs_download = True
    _wire(pipeline, vad=FakeVad(), stt=broken, tts=FakeLocalTTS())
    with download_progress(events.append):
        assert await pipeline.warmup() is False
    assert [e.state for e in events] == ["loading", "error"]
