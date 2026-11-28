"""The image preview opens cleanly and the rain holds still under a modal."""

from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image

from novacode_cli.tui.screens import ImagePreviewScreen
from novacode_cli.tui.widgets import MatrixRain
from tests.test_tui_sessions import _app


def test_rain_does_not_tick_while_a_modal_is_open(monkeypatch, tmp_path):
    """Every rain tick under a modal re-composited the whole screen including
    the preview's full-screen image — the "preview stutters and glitches"
    report. The tick guard must hold the rain still until the modal closes."""
    import novacode_cli.config.config as config_module
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")

    def initialize(config):
        config.config_dir = tmp_path / ".nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {
            "home_style": "classic",
            "home_animations_enabled": True,
            "matrix_rain_enabled": True,
        }
        config._loaded = {}

    async def no_voice_warmup():
        pass

    monkeypatch.setattr(NovaConfig, "__init__", initialize)
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", lambda self: no_voice_warmup())
    asyncio.run(_drive(tmp_path))


async def _drive(tmp_path: Path):
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()
        rain = app._home_banner
        assert isinstance(rain, MatrixRain), "the classic home mounts the rain"
        assert rain._timer is not None, "animations are on, so the rain timer runs"
        for _ in range(10):
            await pilot.pause()
        ticks_before = rain._tick_count
        assert ticks_before > 0, "the rain was ticking before the modal"

        shot = tmp_path / "step-final.png"
        Image.new("RGB", (800, 500), (10, 10, 30)).save(shot)
        screen = ImagePreviewScreen(str(shot))
        app.push_screen(screen)
        for _ in range(40):
            await pilot.pause()

        assert app.screen is screen, "the preview should be the active screen"
        frames_under_modal = rain._frame_count
        await pilot.pause()
        await pilot.pause()
        assert rain._frame_count == frames_under_modal, (
            "the rain kept building frames under the modal"
        )

        # The half-block content actually rendered.
        from textual.widgets import Static

        body = screen.query_one("#image-body", Static)
        rendered = body.render()
        assert rendered is not None
        text = getattr(rendered, "plain", None) or str(rendered)
        assert "Loading image" not in text, "the preview never finished loading"
        assert "Could not load" not in text, f"the preview failed to load: {text[:120]}"
        assert "\u2580" in text, "the half-block image frame did not render"

        app.pop_screen()
        for _ in range(5):
            await pilot.pause()
        assert app.screen is not screen
