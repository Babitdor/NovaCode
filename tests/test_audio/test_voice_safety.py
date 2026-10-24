"""Speech cancellation and input/output coordination without audio hardware."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest

from novacode_cli.audio.pipeline import VoicePipeline
from novacode_cli.audio.tts import Speaker

if TYPE_CHECKING:
    from collections.abc import Iterator


async def test_cancelled_synthesis_cannot_start_playback(monkeypatch: pytest.MonkeyPatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    plays = []

    class Voice:
        def synthesize(self, _text: str) -> Iterator[SimpleNamespace]:
            started.set()
            assert release.wait(2)
            yield SimpleNamespace(audio_int16_array=np.array([1, 2], dtype=np.int16))

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            play=lambda *_a, **_kw: plays.append(True),
            wait=lambda: None,
            stop=lambda: None,
        ),
    )
    speaker = Speaker()
    speaker._voice = Voice()
    original = speaker._speak_sync

    def observe_completion(text: str) -> None:
        try:
            original(text)
        finally:
            finished.set()

    monkeypatch.setattr(speaker, "_speak_sync", observe_completion)
    task = asyncio.create_task(speaker.speak("Should never play after cancellation."))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert plays == []


async def test_pipeline_serializes_speech_and_keeps_echo_guard_active():
    pipeline = VoicePipeline()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class TTS:
        async def speak(self, text: str) -> None:
            calls.append(text)
            started.set()
            await release.wait()

    pipeline._tts = TTS()
    first = asyncio.create_task(pipeline.speak("first"))
    await started.wait()
    second = asyncio.create_task(pipeline.speak("second"))
    await asyncio.sleep(0)
    try:
        assert calls == ["first"]
        assert pipeline.tts_active
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert calls == ["first", "second"]
    assert not pipeline.tts_active


async def test_stopping_listen_during_transcription_never_submits_late_prompt():
    stopped = False
    transcripts = []
    pipeline = VoicePipeline()

    class STT:
        async def transcribe(self, _pcm: np.ndarray) -> str:
            nonlocal stopped
            stopped = True
            return "A late transcript after voice was stopped"

    class VAD:
        async def collect_utterance_async(self, *_a: object, **_kw: object) -> np.ndarray:
            return np.zeros(8, dtype=np.int16)

    pipeline._capture = SimpleNamespace(
        start=lambda: None, stop=lambda: None, drain=lambda: None, read=lambda: None
    )
    pipeline._vad = VAD()
    pipeline._stt = STT()
    await pipeline.listen_loop(transcripts.append, should_stop=lambda: stopped)
    assert transcripts == []


async def test_cancellation_stops_active_owned_playback(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.audio.playback import play_and_wait, run_speech

    started, stopped, finished = threading.Event(), threading.Event(), threading.Event()
    stops = []

    def play(*_args: object, **_kwargs: object) -> None:
        started.set()

    def stop() -> None:
        stops.append(True)
        stopped.set()

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            play=play,
            wait=lambda: stopped.wait(2),
            stop=stop,
        ),
    )

    def operation() -> None:
        try:
            play_and_wait(np.array([1, 2], dtype=np.int16), 16_000)
        finally:
            finished.set()

    task = asyncio.create_task(run_speech(operation))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stops == [True]
    finally:
        stopped.set()
        assert await asyncio.to_thread(finished.wait, 2)


async def test_pipeline_speech_can_be_called_from_another_event_loop():
    pipeline = VoicePipeline()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class TTS:
        async def speak(self, text: str) -> None:
            calls.append(text)
            if text == "first":
                started.set()
                await release.wait()

    pipeline._tts = TTS()
    first = asyncio.create_task(pipeline.speak("first"))
    await started.wait()
    second = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(pipeline.speak("second"))))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["first", "second"]
    assert not pipeline.tts_active


async def test_piper_plays_first_chunk_before_synthesizing_rest(monkeypatch: pytest.MonkeyPatch):
    played = []
    first = np.array([1, 2], dtype=np.int16)
    second = np.array([3, 4], dtype=np.int16)

    class Voice:
        def synthesize(self, _text: str) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(audio_int16_array=first)
            assert len(played) == 1, "First sentence must play before later synthesis"
            yield SimpleNamespace(audio_int16_array=second)

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            play=lambda pcm, *_a, **_kw: played.append(pcm.copy()),
            wait=lambda: None,
            stop=lambda: None,
        ),
    )
    speaker = Speaker()
    speaker._voice = Voice()
    await speaker.speak("First sentence. Second sentence.")
    assert len(played) == 2
    np.testing.assert_array_equal(played[0][:2], first)
    np.testing.assert_array_equal(played[1][:2], second)


async def test_piper_stops_generating_sentences_when_playback_cancelled(
    monkeypatch: pytest.MonkeyPatch,
):
    started, stopped, finished = threading.Event(), threading.Event(), threading.Event()
    generated = []

    class Voice:
        def synthesize(self, _text: str) -> Iterator[SimpleNamespace]:
            try:
                for sentence in range(3):
                    generated.append(sentence)
                    yield SimpleNamespace(audio_int16_array=np.array([1, 2], dtype=np.int16))
            finally:
                finished.set()

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            play=lambda *_a, **_kw: started.set(),
            wait=lambda: stopped.wait(2),
            stop=stopped.set,
        ),
    )
    speaker = Speaker()
    speaker._voice = Voice()
    task = asyncio.create_task(speaker.speak("One. Two. Three."))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        stopped.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert generated == [0]
