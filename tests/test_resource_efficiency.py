"""Resource contracts for concurrent text sessions, without native models."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from novacode_cli.audio.pipeline import VoicePipeline
from novacode_cli.tui.app import NovaApp

if TYPE_CHECKING:
    from pathlib import Path

    from novacode_cli.config.nova_config import NovaConfig

START_VOICE = NovaApp._eager_voice_warmup


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep configuration reads/writes independent of concurrent live sessions."""
    from novacode_cli.config.nova_config import NovaConfig

    def initialize(config: NovaConfig) -> None:
        config.config_dir = tmp_path / "nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {}
        config._loaded = {}

    monkeypatch.setattr(NovaConfig, "__init__", initialize)


class EmptyAgent:
    async def aget_state(self, _config: object):
        return SimpleNamespace(values={"messages": []})


def app():
    return NovaApp(
        agent=EmptyAgent(),
        assistant_id="resource-test",
        backend=None,
        session_state=SimpleNamespace(
            thread_id="resource-test", todos=None, plan_mode_enabled=False, steering_instructions=[]
        ),
        token_tracker=None,
        image_tracker=None,
        model_name="test",
    )


async def test_disabled_voice_does_not_construct_pipeline(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(
        NovaConfig,
        "get_voice_config",
        lambda _self: {
            "enabled": False,
            "mode": "push_to_talk",
            "speak_responses": True,
        },
    )
    nova = app()
    calls = []
    monkeypatch.setattr(nova, "_ensure_voice_pipeline", lambda: calls.append("voice"))
    await START_VOICE(nova)
    assert calls == []
    assert nova._voice_pipeline is None


def test_voice_activation_does_not_preload_unused_models(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli import audio
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(audio, "is_voice_available", lambda: True)
    monkeypatch.setattr(
        NovaConfig, "get_voice_config", lambda _self: dict(NovaConfig.VOICE_DEFAULTS)
    )
    nova = app()
    warmups = []
    monkeypatch.setattr(nova, "_voice_warmup", lambda: warmups.append("warmup"))
    assert nova._ensure_voice_pipeline() is True
    assert nova._voice_pipeline is not None
    assert warmups == []


async def test_speech_only_does_not_build_input_stack(monkeypatch: pytest.MonkeyPatch):
    pipeline = VoicePipeline()
    spoken = []

    async def speak(text: str) -> None:
        spoken.append(text)

    monkeypatch.setattr(
        pipeline, "_build_tts", lambda: SimpleNamespace(speak=speak, needs_download=False)
    )
    assert pipeline.tts_needs_download is False
    await pipeline.speak("hello")
    assert spoken == ["hello"]
    assert pipeline._stt is None
    assert pipeline._vad is None
    assert pipeline._capture is None
    assert pipeline.tts_active is False


async def test_speech_failure_resets_active_flag():
    pipeline = VoicePipeline()

    async def fail(_text: str) -> None:
        message = "provider failed"
        raise RuntimeError(message)

    pipeline._tts = SimpleNamespace(speak=fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        await pipeline.speak("hello")
    assert pipeline.tts_active is False


async def test_idle_session_has_no_continuous_banner_animation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NOVA_ANIMATIONS", raising=False)
    nova = app()
    count = 0
    tick = nova._tick

    def counted_tick() -> None:
        nonlocal count
        count += 1
        tick()

    monkeypatch.setattr(nova, "_tick", counted_tick)
    async with nova.run_test() as pilot:
        await pilot.pause()
        count = 0
        await asyncio.sleep(1.1)
        assert count <= 3
        assert nova._home_banner._timer is None
        assert nova._home_banner._strips  # still renders the themed logo


async def test_banner_animation_remains_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NOVA_ANIMATIONS", "1")
    nova = app()
    async with nova.run_test() as pilot:
        await pilot.pause()
        assert nova._home_banner._timer is not None


async def test_static_banner_reflows_and_recolors(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.config import get_responsive_ascii

    monkeypatch.delenv("NOVA_ANIMATIONS", raising=False)
    nova = app()
    async with nova.run_test() as pilot:
        await pilot.pause()
        rain = nova._home_banner
        rain.reflow(get_responsive_ascii(width=140), 140)
        assert rain._strips[0].cell_length == rain._col_count
        before = rain._palette_key
        nova.theme = "matrix" if nova.theme != "matrix" else "flexoki"
        await pilot.pause()
        assert rain._palette_key == rain._theme_key()
        assert rain._palette_key != before


def test_banner_can_be_measured_before_mount():
    from novacode_cli.tui.widgets import MatrixRain

    assert "ready" in MatrixRain(art="ready", width=80).render().plain


async def test_tts_warmup_leaves_input_unallocated(monkeypatch: pytest.MonkeyPatch):
    pipeline = VoicePipeline()
    loaded = []
    monkeypatch.setattr(
        pipeline,
        "_build_tts",
        lambda: SimpleNamespace(
            _ensure_voice=lambda: loaded.append("tts"),
        ),
    )
    await pipeline.warmup(input_audio=False)
    assert loaded == ["tts"]
    assert pipeline._capture is pipeline._vad is pipeline._stt is None


async def test_active_status_is_prompt_and_slows_when_unfocused(monkeypatch: pytest.MonkeyPatch):
    nova = app()
    count = 0
    tick = nova._tick

    def counted_tick() -> None:
        nonlocal count
        count += 1
        tick()

    monkeypatch.setattr(nova, "_tick", counted_tick)
    async with nova.run_test() as pilot:
        await pilot.pause()
        nova._turn_active = True
        nova._set_status("thinking")
        count = 0
        await asyncio.sleep(0.24)
        assert 1 <= count <= 3
        nova.on_app_blur()
        count = 0
        await asyncio.sleep(1.1)
        assert count <= 3
        nova.on_app_focus()
        count = 0
        await asyncio.sleep(0.24)
        assert 1 <= count <= 3
        nova._turn_active = False
        nova._set_status("ready")


def test_native_thread_defaults_and_user_overrides(monkeypatch: pytest.MonkeyPatch):
    import importlib
    import os

    import novacode_cli

    names = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    importlib.reload(novacode_cli)
    assert all(os.environ.get(name) == "1" for name in names)
    for name in names:
        monkeypatch.setenv(name, "3")
    importlib.reload(novacode_cli)
    assert all(os.environ[name] == "3" for name in names)


def test_tool_backlog_is_bounded_before_flush(monkeypatch: pytest.MonkeyPatch):
    nova = app()
    scheduled = []
    monkeypatch.setattr(nova, "_post_to_ui", lambda *args: scheduled.append(args))
    for _ in range(200):
        nova._on_tool_output("call", "x" * 10_000)
    buffer = nova._tool_out_pending["call"]
    # Existing list-of-chunks storage demonstrates the regression as well.
    stored = sum(map(len, buffer)) if isinstance(buffer, list) else len(buffer)
    assert stored <= 65_536
    assert len(scheduled) == 1


def test_tool_backlog_bounds_call_count_and_keeps_newest_tail(monkeypatch: pytest.MonkeyPatch):
    nova = app()
    monkeypatch.setattr(nova, "_post_to_ui", lambda *_args: None)
    for index in range(200):
        nova._on_tool_output(str(index), "x" * 100_000 + f" END-{index}")
    assert len(nova._tool_out_pending) <= 32
    painted = {}
    monkeypatch.setattr(nova, "_write_tool_output", lambda cid, text: painted.update({cid: text}))
    nova._flush_tool_output()
    assert painted["199"].endswith(" END-199")
    assert "truncated" in painted["199"].lower()
    assert nova._tool_out_pending == {}
    assert nova._tool_out_scheduled is False


def test_concurrent_output_and_flush_remains_usable(monkeypatch: pytest.MonkeyPatch):
    nova = app()
    monkeypatch.setattr(nova, "_post_to_ui", lambda *_args: None)
    painted = []
    monkeypatch.setattr(nova, "_write_tool_output", lambda cid, text: painted.append((cid, text)))

    def produce(index: int) -> None:
        for n in range(100):
            nova._on_tool_output(str(index), f"{n}\n" + "x" * 1_000)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(produce, index) for index in range(4)]
        while not all(f.done() for f in futures):
            nova._flush_tool_output()
        for future in futures:
            future.result()
    nova._on_tool_output("final", "sentinel")
    nova._flush_tool_output()
    assert ("final", "sentinel") in painted
    assert nova._tool_out_pending == {}
    assert nova._tool_out_scheduled is False


def test_output_tail_matches_reference_under_adversarial_chunks():
    import random

    from novacode_cli.tui.output_buffer import MAX_PENDING_CHARS, OutputTail

    rng = random.Random(20261006)
    buffer = OutputTail()
    full = ""
    for size in [
        0,
        1,
        MAX_PENDING_CHARS,
        MAX_PENDING_CHARS + 1,
        *[rng.randrange(100_000) for _ in range(60)],
    ]:
        chunk = "🦉" * size
        buffer.append(chunk)
        full += chunk
        assert len(buffer) == min(len(full), MAX_PENDING_CHARS)
    assert buffer.drain().endswith(full[-MAX_PENDING_CHARS:])
    assert len(buffer) == 0
    assert buffer.drain() == ""
    buffer.append("sentinel")
    assert buffer.drain() == "sentinel"
