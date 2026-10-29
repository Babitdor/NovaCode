"""Lite Mode presents one explicit style, preserves preferences, and keeps drafts local."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Button, Select, SelectionList, Static

from novacode_cli.config.nova_config import NovaConfig
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app


def test_public_commands_leave_skill_names_available():
    from novacode_cli.commands import build_command_registry
    from novacode_cli.tui.app import TUI_COMMANDS

    registry = build_command_registry()
    assert "lite-mode" in TUI_COMMANDS
    assert "caveman" not in TUI_COMMANDS
    assert "ponytail" not in TUI_COMMANDS
    assert registry.get("lite-mode") is not None


def test_off_retains_last_style_and_raw_feature_choices():
    config = NovaConfig()
    config.set_caveman_features(dict.fromkeys(config.get_caveman_features(), False))
    config.set_compression_mode("ponytail")
    config.set_ponytail_level("ultra")
    config.set_compression_mode("off")
    reloaded = NovaConfig()
    assert reloaded.get_preferred_compression_mode() == "ponytail"
    assert reloaded.get_ponytail_level() == "ultra"
    assert not any(reloaded.get_caveman_feature_preferences().values())


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(100, 34), (40, 24)])
async def test_settings_show_only_the_selected_style_and_keep_actions_visible(
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    monkeypatch.setattr(app, "_run_skill", AsyncMock(return_value=False))
    monkeypatch.setattr(app, "_run_plugin_command", AsyncMock(return_value=False))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.text = "Keep my unfinished prompt"
        await app._run_slash("/lite-mode settings")
        await pilot.pause()
        screen = app.screen
        assert "Lite Mode" in str(screen.query_one("#modal-title", Static).render())
        assert not screen.query_one("#caveman-section").display
        assert not screen.query_one("#ponytail-section").display
        mode = screen.query_one("#compression-mode", Select)
        mode.value = "caveman"
        await pilot.pause()
        assert screen.query_one("#caveman-section").display
        assert not screen.query_one("#ponytail-section").display
        screen.query_one("#caveman-level", Select).value = "wenyan-full"
        mode.value = "ponytail"
        await pilot.pause()
        assert not screen.query_one("#caveman-section").display
        assert screen.query_one("#ponytail-section").display
        screen.query_one("#ponytail-level", Select).value = "ultra"
        assert NovaConfig().get_compression_mode() == "off"
        for selector in ("#caveman-save", "#caveman-cancel"):
            button = screen.query_one(selector, Button)
            assert button.region.bottom <= size[1]
            assert button.region.right <= size[0]
            assert button.region.height >= 3
        await pilot.press("escape")
        await pilot.pause()
        assert NovaConfig().get_compression_mode() == "off"
        assert NovaConfig().get_ponytail_level() == "full"
        assert prompt.text == "Keep my unfinished prompt"


@pytest.mark.timeout(30)
async def test_composer_toggles_last_style_without_cycling_or_submitting(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.commands.lite_mode_handler import handle_lite_mode_command

    config = NovaConfig()
    config.set_compression_mode("ponytail")
    config.set_ponytail_level("ultra")
    config.set_compression_mode("off")
    app = _app()
    stream = AsyncMock()
    monkeypatch.setattr(app, "_stream_prompt", stream)
    async with app.run_test(size=(55, 25)) as pilot:
        await pilot.pause()
        app.query_one("#prompt").text = "Do not send this draft"
        button = app.query_one("#caveman-toggle", Button)
        assert not app.query("#caveman-options")
        assert str(button.label) == "lite:off"
        for expected in ("ponytail", "off", "ponytail"):
            button.focus()
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert NovaConfig().get_compression_mode() == expected
        assert str(button.label) == "lite:ponytail"
        assert NovaConfig().get_ponytail_level() == "ultra"
        assert app.query_one("#prompt").text == "Do not send this draft"
        stream.assert_not_called()
        assert "off" in handle_lite_mode_command("off")
        assert "Ponytail" in handle_lite_mode_command("status")


def test_mode_commands_are_atomic_and_invalid_choices_preserve_preferences():
    from novacode_cli.commands.lite_mode_handler import handle_lite_mode_command

    assert "Ponytail" in handle_lite_mode_command("ponytail ultra")
    config = NovaConfig()
    assert config.get_compression_mode() == "ponytail"
    assert config.get_ponytail_level() == "ultra"
    assert "Usage" in handle_lite_mode_command("ponytail wenyan-full")
    assert NovaConfig().get_compression_mode() == "ponytail"
    assert NovaConfig().get_ponytail_level() == "ultra"
    assert "Caveman" in handle_lite_mode_command("caveman lite")
    assert NovaConfig().get_caveman_level() == "lite"
    assert NovaConfig().get_ponytail_level() == "ultra"


@pytest.mark.timeout(30)
async def test_apply_and_storage_failure_keep_the_effective_state(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    async with app.run_test(size=(55, 25)) as pilot:
        await pilot.pause()
        await app._run_slash("/lite-mode settings")
        await pilot.pause()
        app.screen.query_one("#compression-mode", Select).value = "caveman"
        await pilot.pause()
        app.screen.query_one(SelectionList).deselect_all()
        app.screen.query_one("#caveman-level", Select).value = "lite"
        await pilot.click("#caveman-save")
        await pilot.pause()
        assert NovaConfig().get_compression_mode() == "caveman"
        assert not any(NovaConfig().get_caveman_feature_preferences().values())
        monkeypatch.setattr(
            NovaConfig, "set_compression_mode", Mock(side_effect=OSError("read only"))
        )
        await app._run_slash("/lite-mode off")
        assert NovaConfig().get_compression_mode() == "caveman"
        assert str(app.query_one("#caveman-toggle", Button).label) == "lite:caveman"
