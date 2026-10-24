"""Voice download progress remains visible and responsive inside the composer."""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Button, ProgressBar, Static

from novacode_cli.audio.downloads import ModelDownload, watch_downloads
from novacode_cli.tui.voice_downloads import VoiceDownloads
from tests.test_audio.test_orpheus_resources import orpheus_runtime  # noqa: F401 -- pytest fixture.
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app


@pytest.mark.parametrize("size", [(80, 30), (45, 25)])
async def test_model_bytes_unknown_totals_and_failure_display(size: tuple[int, int]):
    app = _app()
    async with app.run_test(size=size) as pilot:
        panel = app.query_one(VoiceDownloads)
        assert not panel.display
        event = ModelDownload("stt", "STT", "whistle", "loading")
        app._post_voice_download_progress(event)
        await pilot.pause()
        assert panel.display
        assert app.query_one("#voice-stt-download-bar", ProgressBar).total is None
        app._post_voice_download_progress(
            replace(event, state="downloading", filename="whistle.cact", completed=50, total=100)
        )
        tts = ModelDownload("tts", "TTS", "piper", "downloading", "amy.onnx", 25)
        app._post_voice_download_progress(tts)
        await pilot.pause()
        bar = app.query_one("#voice-stt-download-bar", ProgressBar)
        assert bar.progress == 50
        assert bar.total == 100
        assert "50 bytes / 100 bytes" in str(
            app.query_one("#voice-stt-download-label", Static).render()
        )
        assert app.query_one("#voice-tts-download-bar", ProgressBar).total is None
        assert app.query_one("#prompt").region.bottom <= size[1]
        app._post_voice_download_progress(
            replace(event, state="error", error="Network unavailable")
        )
        await pilot.pause()
        assert "failed: Network unavailable" in str(
            app.query_one("#voice-stt-download-label", Static).render()
        )
        assert not bar.display


async def test_old_pipeline_and_old_operation_cannot_overwrite_progress():
    app = _app()
    async with app.run_test() as pilot:
        owner = object()
        app._voice_pipeline = owner
        old = ModelDownload("old", "STT", "whistle", "loading")
        new = ModelDownload("new", "STT", "whistle", "loading")
        app._post_voice_download_progress(old, owner)
        app._post_voice_download_progress(new, owner)
        app._post_voice_download_progress(replace(old, state="ready"), owner)
        app._post_voice_download_progress(
            replace(old, state="downloading", completed=99, total=100), owner
        )
        await pilot.pause()
        label = app.query_one("#voice-stt-download-label", Static)
        assert "preparing model" in str(label.render())
        app._voice_pipeline = object()
        app._post_voice_download_progress(
            replace(new, state="downloading", completed=10, total=100), owner
        )
        await pilot.pause()
        assert not app.query_one(VoiceDownloads).display


async def test_cancelled_download_hides_only_its_row():
    app = _app()
    async with app.run_test() as pilot:
        stt = ModelDownload("stt", "STT", "whistle", "loading")
        tts = ModelDownload("tts", "TTS", "piper", "loading")
        for event in (stt, tts, replace(stt, state="cancelled")):
            app._post_voice_download_progress(event)
        await pilot.pause()
        assert not app.query_one("#voice-stt-download").display
        assert app.query_one("#voice-tts-download").display
        assert app.query_one(VoiceDownloads).display


async def test_worker_progress_renders_while_talk_action_waits_for_model(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fetch(*, tqdm_class: type[Any] | None = None) -> None:
        assert tqdm_class is not None
        try:
            with tqdm_class(total=100, unit="B", desc="whistle.cact") as bar:
                started.set()
                assert release.wait(3)
                bar.update(100)
        finally:
            finished.set()

    hub = SimpleNamespace(hf_hub_download=fetch)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    class Pipeline:
        async def capture_utterance(self) -> None:
            async with watch_downloads(
                "STT",
                "whistle",
                SimpleNamespace(needs_download=True),
                lambda event: app._post_voice_download_progress(event, self),
            ):
                await asyncio.to_thread(hub.hf_hub_download)

    async with app.run_test() as pilot:
        app._voice_pipeline = Pipeline()
        monkeypatch.setattr(app, "_ensure_voice_pipeline", lambda: True)
        press = asyncio.create_task(pilot.press("ctrl+g"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(0.1)
            assert app.query_one(VoiceDownloads).display
            assert "whistle.cact" in str(
                app.query_one("#voice-stt-download-label", Static).render()
            )
        finally:
            release.set()
            await press
            assert await asyncio.to_thread(finished.wait, 2)


async def test_settings_download_keeps_keyboard_live_during_orpheus_initialization(
    orpheus_runtime: SimpleNamespace,  # noqa: F811 -- imported pytest fixture.
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.audio.vad import SileroVad
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tui.screens import SettingsScreen

    app = _app()
    started, release = threading.Event(), threading.Event()
    constructor = sys.modules["llama_cpp"].Llama

    def loading(**kwargs: Any) -> object:
        started.set()
        assert release.wait(5)
        return constructor(**kwargs)

    async def no_vad(_self: SileroVad) -> None:
        pass

    monkeypatch.setattr(sys.modules["llama_cpp"], "Llama", loading)
    monkeypatch.setattr(SileroVad, "ensure_model_async", no_vad)
    monkeypatch.setattr(
        "novacode_cli.commands.voice_handler.audio.is_voice_available", lambda: True
    )
    config = NovaConfig()
    config.set_voice_config(tts_provider="orpheus", stt_provider="deepgram")
    config.set_voice_provider_config("deepgram", api_key="test-placeholder")
    async with app.run_test(size=(120, 50)) as pilot:
        screen = SettingsScreen(section="voice")
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#voice-download", Button).press()
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await pilot.pause()
            label = app._voice_downloads_panel.query_one("#voice-tts-download-label", Static)
            assert "preparing model" in str(label.render())
            assert len(orpheus_runtime.downloads) == 1  # GGUF finished; native load still waiting.
            await asyncio.wait_for(pilot.press("escape"), timeout=1)
            assert app.screen is not screen
            prompt = app.screen.query_one("#prompt")
            prompt.focus()
            await asyncio.wait_for(pilot.press("x"), timeout=1)
            assert prompt.text == "x"
        finally:
            release.set()
            for worker in list(app.workers):
                if worker.group == "voice_cmd":
                    await asyncio.wait_for(worker.wait(), timeout=3)
        await pilot.pause()
        assert "ready" in str(label.render())
        assert orpheus_runtime.llms[0]["n_ctx"] == 4096
