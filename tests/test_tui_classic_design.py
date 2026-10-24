"""Classic's full canvas and design changes preserve the interactive composer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.color import Color
from textual.widgets import OptionList, Select

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.home import HOME_STYLES
from novacode_cli.tui.screens import ConfirmModal, ThemeScreen
from novacode_cli.tui.widgets import MatrixRain, PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


async def test_classic_canvas_fills_viewport_and_follows_composer(tmp_path: Path) -> None:
    NovaConfig().set("home_style", "classic")
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        assert app.theme == "nova-classic"
        rain = app.query_one(MatrixRain)
        prompt = app.query_one(PromptInput)
        assert rain.size.width >= app.size.width - 6
        assert rain.size.height >= app.size.height - 7
        assert rain._col_count == rain.size.width
        assert rain._row_count == rain.size.height
        assert rain._art_left == 1
        assert rain._art_top + len(rain._art_lines) <= rain.size.height
        assert rain.region.bottom <= app.query_one("#prompt-dock").region.y
        assert app.query_one("#prompt-row").styles.border_top[0] == "solid"
        assert set(rain._chars) == set("0123456789")
        assert all(
            segment.style.bgcolor == Color.parse(app.current_theme.background)
            for strip in rain._strips
            for segment in strip
        )
        (tmp_path / "classic.svg").write_text(app.export_screenshot(), encoding="utf-8")
        previous_height = rain.size.height
        prompt.value = "First line\nSecond line\nThird line"
        await pilot.pause()
        assert rain.size.height < previous_height
        assert rain._row_count == rain.size.height
        for width, height in ((85, 32), (48, 20), (40, 16), (170, 50)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert prompt.region.bottom <= height
            assert prompt.size.height >= 3
            assert prompt.value == "First line\nSecond line\nThird line"
            if height >= 20:
                assert rain._col_count == rain.size.width
                assert rain._row_count == rain.size.height
        await pilot.resize_terminal(85, 30)
        prompt.value = "\n".join(f"Line {index}" for index in range(12))
        await pilot.pause()
        assert rain._art_top + len(rain._art_lines) <= rain.size.height
        assert rain._art_left + rain._art_w <= rain.size.width
        app._set_home_visible(visible=False)
        await pilot.pause()
        assert app.query_one("#workspace-layout").size.height > 0
        assert app.query_one("#info-bar").size.height > 0
        assert app.screen.has_class("classic-ui")


@pytest.mark.timeout(40)
async def test_design_switch_updates_composer_and_stacked_dialogs() -> None:
    app = _app()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        root = app.screen
        prompt = app.query_one(PromptInput)
        prompt.value = "Keep this draft\nwith its second line"
        app._set_home_visible(visible=False)
        app.push_screen(ConfirmModal("Keep this dialog open?", "Design preview"))
        await pilot.pause()
        underneath = app.screen
        settings = ThemeScreen()
        app.push_screen(settings)
        await pilot.pause()
        expected_border = {
            "classic": "solid",
            "centered": "round",
            "banner": "round",
            "minimal": "",
            "dashboard": "round",
            "terminal": "solid",
            "workbench": "solid",
            "notebook": "",
            "outline": "ascii",
            "focus": "solid",
            "studio": "round",
            "ledger": "double",
            "blueprint": "double",
        }
        for _, style in HOME_STYLES:
            settings.query_one("#home-style-select", Select).value = style
            await pilot.pause()
            for screen in (root, underneath, settings):
                assert screen.has_class(f"{style}-ui")
                assert sum(screen.has_class(f"{key}-ui") for _, key in HOME_STYLES) == 1
            assert app.query_one("#prompt-row").styles.border_top[0] == expected_border[style]
            if style in {"classic", "centered"}:
                assert (
                    settings.query_one("SelectCurrent").styles.border_top[0]
                    == expected_border[style]
                )
            assert prompt.value == "Keep this draft\nwith its second line"
            assert not app._home_visible
        settings.dismiss()
        await pilot.pause()
        assert underneath.has_class("minimal-ui")
        underneath.dismiss(result=False)
        await pilot.pause()
        assert app.query_one(PromptInput) is prompt


@pytest.mark.parametrize("palette", [None, "nova-paper"])
async def test_classic_default_palette_preserves_explicit_choice(palette: str | None) -> None:
    if palette:
        NovaConfig().set("theme", palette)
    app = _app()
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        original = app.theme
        app._set_home_style("classic")
        await pilot.pause()
        assert app.theme == (palette or "nova-classic")
        app._set_home_style("dashboard")
        await pilot.pause()
        assert app.theme == original
        assert NovaConfig().get("theme") == palette


async def test_palette_chosen_after_mount_survives_design_switch() -> None:
    app = _app()
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        app._set_home_style("classic")
        await pilot.pause()
        NovaConfig().set("theme", "nova-classic")
        app._set_home_style("dashboard")
        await pilot.pause()
        assert app.theme == "nova-classic"


@pytest.mark.timeout(40)
async def test_completion_menu_tracks_each_design_and_multiline_drafts() -> None:
    app = _app()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        menu = app.query_one("#cmdpalette", OptionList)
        prompt = app.query_one(PromptInput)
        for _, style in HOME_STYLES:
            app._set_home_style(style)
            prompt.value = "Keep this draft\nand this line\nand another"
            await pilot.pause()
            menu.clear_options()
            menu.add_options(["/help", "/home"])
            menu.display = True
            await pilot.pause()
            app._position_palette()
            await pilot.pause()
            assert menu.region.bottom <= app.query_one("#prompt-dock").region.y
            assert menu.region.y >= 0
            menu.display = False


async def test_dashboard_cards_actions_and_responsive_composer(tmp_path: Path) -> None:
    from novacode_cli.tui.home import SystemSnapshot

    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        app._show_home_system(SystemSnapshot(25, 50, 1024, 2048))
        await pilot.pause()
        assert app._home_banner._row_count <= 12
        assert "What are we building?" in str(app.query_one("#home-intro").render())
        assert "test-model" in str(app.query_one("#home-model").render())
        assert "25%" in str(app.query_one("#home-system").render())
        (tmp_path / "dashboard.svg").write_text(app.export_screenshot(), encoding="utf-8")
        actions = [
            app.query_one(f"#home-action-{key}")
            for key in ("explain", "refactor", "build", "debug")
        ]
        assert actions[0].region.y == actions[1].region.y
        assert actions[2].region.y > actions[0].region.y
        assert actions[3].region.right <= app.query_one("#conversation-column").region.right
        actions[3].focus()
        await pilot.press("enter")
        prompt = app.query_one(PromptInput)
        assert prompt.value == "Investigate and fix this issue: "
        assert prompt.has_focus
        for width, height in ((100, 44), (70, 30), (48, 16), (170, 50)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert prompt.region.bottom <= height
            assert prompt.size.width > 0
            assert prompt.value == "Investigate and fix this issue: "
            if height >= 40:
                assert actions[3].region.bottom <= app.query_one("#prompt-dock").region.y
