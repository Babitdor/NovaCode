"""Animation preferences apply live without changing motion speed or idle work."""

from types import SimpleNamespace

import pytest
from textual.widgets import Select, Switch

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.animation_rate import animation_fps
from novacode_cli.tui.screens import SettingsScreen
from novacode_cli.tui.widgets import MatrixRain
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.fixture(autouse=True)
def load_saved_settings(monkeypatch, isolated_session_config):  # noqa: F811 — fixture dependency
    initialize = NovaConfig.__init__

    def initialize_and_load(config):
        initialize(config)
        config._load()

    monkeypatch.setattr(NovaConfig, "__init__", initialize_and_load)


@pytest.mark.parametrize("value", [None, True, "60", 0, 120, 30.0])
def test_invalid_saved_rates_use_balanced_default(value):
    assert animation_fps(value) == 30


async def test_settings_rate_persists_and_applies_to_existing_rain_and_low_resource_mode():
    app = _app()
    async with app.run_test(size=(110, 42)) as pilot:
        screen = SettingsScreen()
        app.push_screen(screen)
        await pilot.pause(0.1)
        rain = app._home_banner
        assert rain._timer is None
        screen.query_one("#animation-fps", Select).value = 60
        await pilot.pause(0.1)
        assert app._animation_fps == 60
        assert rain._animation_fps == 60
        assert rain._timer is None, "changing FPS must not enable decorative animation"
        assert NovaConfig().get("animation_fps") == 60
        app._set_matrix_rain_enabled(True)
        assert rain._timer._interval == pytest.approx(1 / 60)
        old_timer = rain._timer
        screen.query_one("#low-resource-toggle", Switch).value = True
        await pilot.pause(0.1)
        assert app._low_resource_mode is True
        assert rain._timer is not old_timer
        assert rain._timer._interval == pytest.approx(1 / 15)
        screen.query_one("#low-resource-toggle", Switch).value = False
        await pilot.pause(0.1)
        assert rain._timer._interval == pytest.approx(1 / 60)
        rain.set_animation_enabled(False)
        screen.dismiss()
        app._load_ui_preferences()
        assert app._animation_fps == 60


async def test_active_rate_honors_preference_focus_and_low_resource():
    app = _app()
    async with app.run_test():
        app._turn_active = True
        app._os_focused = True
        for fps in (15, 30, 60):
            app._set_animation_fps(fps)
            assert app._status_timer._interval == pytest.approx(1 / fps)
        app._low_resource_mode = True
        app._schedule_status_tick()
        assert app._status_timer._interval == pytest.approx(0.2)
        app._os_focused = False
        app._schedule_status_tick()
        assert app._status_timer._interval == pytest.approx(0.5)
        app._turn_active = False


def test_rain_moves_at_same_speed_across_rate_choices():
    positions = []
    for fps in (15, 30, 60):
        rain = MatrixRain(art="NOVA", width=80, fps=fps)
        rain._init_columns()
        rain._columns = [{"pos": 0.0, "speed": 0.1, "trail": 5}]
        for _ in range(fps):
            rain._fill_buffers()
        positions.append(rain._columns[0]["pos"])
    assert positions == pytest.approx([1.5, 1.5, 1.5])


async def test_animation_clock_does_not_multiply_background_polling(monkeypatch):
    app = _app()
    async with app.run_test():
        app._turn_active = True
        app._os_focused = True
        app._set_animation_fps(60)
        # Drive a reproducible animation clock, independent of test CPU speed.
        from novacode_cli.tui import app as app_module

        clock = [1000.0]
        monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(app, "_refresh_status", lambda: None)
        polls = []
        monkeypatch.setattr(app, "_unread_count", lambda: polls.append(clock[0]) or 0)
        app._last_background_poll = 0.0
        for n in range(60):
            clock[0] = 1000 + n / 60
            app._tick()
        assert len(polls) <= 4
        assert app._spinner_frame == int(clock[0] * 60)
        app._turn_active = False
