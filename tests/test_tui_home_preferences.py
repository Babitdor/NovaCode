"""Home layout preferences apply live and survive restart."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from textual.widgets import Static, Switch

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.states.Session import SessionState
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.screens import SettingsScreen
from novacode_cli.tui.widgets import MatrixRain
from novacode_cli.ui.ui_elements import TokenTracker
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_sessions import _FakeAgent


def _app() -> NovaApp:
    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=SessionState(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )


@pytest.fixture(autouse=True)
def no_system_sampling(monkeypatch: pytest.MonkeyPatch) -> Mock:
    sampler = Mock()
    monkeypatch.setattr(NovaApp, "_sample_home_system", sampler)
    return sampler


@pytest.mark.parametrize("saved", [None, True, False, "false"])
async def test_saved_home_layout_is_loaded(saved: object, no_system_sampling: Mock) -> None:
    if saved is not None:
        NovaConfig().set("home_dashboard_enabled", saved)
    redesigned = saved if isinstance(saved, bool) else True
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        assert app._home_dashboard_enabled is redesigned
        assert app.screen.has_class("home") is redesigned
        assert app.screen.has_class("classic-home") is not redesigned
        assert (app.query_one("#home-right").size.width > 0) is redesigned
        assert app._home_banner._compact is redesigned
        if not redesigned:
            no_system_sampling.assert_not_called()
            assert app.query_one("#home-banner-caption").size.height == 0
            assert app.query_one("#conversation-column").size.width == app.size.width


async def test_settings_switches_home_live_and_persists() -> None:
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        root = app.screen
        banner = app._home_banner
        marker = Static("Keep this transcript entry")
        await app._transcript().mount(marker)
        screen = SettingsScreen()
        app.push_screen(screen)
        await pilot.pause()
        toggle = screen.query_one("#home-dashboard-toggle", Switch)
        assert toggle.value is True
        for enabled in (False, True, False):
            toggle.value = enabled
            await pilot.pause()
            assert root.has_class("home") is enabled
            assert root.has_class("classic-home") is not enabled
            assert not screen.has_class("home")
            assert not screen.has_class("classic-home")
            assert marker.is_mounted
            assert app._home_banner is banner
            assert len(app.query(MatrixRain)) == 1
            assert banner._compact is enabled
            assert NovaConfig().get("home_dashboard_enabled") is enabled
        screen.dismiss()
        await pilot.pause()
        assert app.query_one("#home-left").size.width == 0
        assert app.query_one("#home-right").size.width == 0
        assert app.query_one("#home-header").size.height > 0
        assert app.query_one("#prompt-dock").size.width == app.size.width

    restarted = _app()
    async with restarted.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        assert restarted.screen.has_class("classic-home")
        assert restarted._home_banner._compact is False


async def test_switch_during_chat_keeps_home_hidden_until_new_chat() -> None:
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        app._set_home_visible(visible=False)
        marker = Static("An ongoing conversation")
        await app._transcript().mount(marker)
        root = app.screen
        screen = SettingsScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#home-dashboard-toggle", Switch).value = False
        await pilot.pause()
        assert not root.has_class("home")
        assert not root.has_class("classic-home")
        assert marker.is_mounted
        screen.dismiss()
        await pilot.pause()
        assert app.query_one("#home-header").size.height == 0
        app._show_home_banner()
        await pilot.pause()
        assert root.has_class("classic-home")
        await pilot.resize_terminal(70, 20)
        await pilot.pause()
        assert app.query_one("#home-header").size.height == 0
        assert app.query_one("#prompt").size.height > 0


async def test_late_system_sample_after_panel_removal_is_ignored() -> None:
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        await app.query_one("#home-system").remove()
        app._show_home_system(None)
