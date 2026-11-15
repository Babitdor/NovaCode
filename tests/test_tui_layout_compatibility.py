"""Compact controls remain visible under Nova's real app styles and color themes."""

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button

from novacode_cli.rooms.store import RoomStore
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.command_center import CommandCenterScreen, CommandSession
from novacode_cli.tui.home import HOME_STYLES
from novacode_cli.tui.room_settings import RoomSettingsModal
from novacode_cli.tui.rooms import RoomsScreen
from novacode_cli.tui.themes import NOVA_EXTRA_THEMES
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_home_preferences import _resize_terminal

STYLES = [style for _, style in HOME_STYLES]
THEMES = ("nova-classic", "nova-synthwave", "textual-light")
SIZES = ((140, 36), (80, 24), (55, 20), (40, 14))


class LayoutHost(App):
    CSS = NovaApp.CSS
    CSS_PATH = Path(__file__).resolve().parents[1] / "novacode_cli/tui/interface_styles.tcss"

    def __init__(self, style):
        super().__init__()
        self._home_style = style
        for theme in NOVA_EXTRA_THEMES:
            self.register_theme(theme)

    def apply_style(self):
        NovaApp._apply_interface_style(self)


def assert_buttons_fit(container):
    buttons = [button for button in container.query(Button) if button.display]
    for button in buttons:
        assert button.content_region.height >= 1, (button.id, button.region, button.styles.border)
        assert button.content_region.width >= 1, (button.id, button.region)
        assert container.content_region.contains_region(button.region), (
            button.id,
            button.region,
            container.content_region,
        )
    for index, first in enumerate(buttons):
        for second in buttons[index + 1 :]:
            assert not first.region.overlaps(second.region), (first.id, second.id)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.timeout(60)
async def test_command_center_filter_labels_fit_all_presets_themes_and_sizes(style):
    screen = CommandCenterScreen(
        None,
        lambda: [
            CommandSession(key="pane:root", pane_id="root", title="Task", status="Ready"),
        ],
    )
    app = LayoutHost(style)
    async with app.run_test(size=SIZES[0]) as pilot:
        await app.push_screen(screen)
        app.apply_style()
        for theme in THEMES:
            app.theme = theme
            for size in SIZES:
                await _resize_terminal(pilot, *size)
                filters = screen.query_one("#cc-filters")
                assert_buttons_fit(filters)
                for tab in filters.query(Button):
                    assert tab.content_region.width >= len(str(tab.label)) + 2 * tab.styles.line_pad
                button = screen.query_one("#cc-filter-1", Button)
                button.focus()
                await pilot.pause()
                assert_buttons_fit(filters)
                assert await pilot.click(button)
                assert screen._filter_index == 1
                assert screen.query_one("#cc-body").region.height > 0


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.timeout(60)
async def test_rooms_headers_toolbar_and_settings_fit_presets_themes_and_sizes(tmp_path, style):
    store = RoomStore(tmp_path)
    store.update_channel("general", "general", "A detailed channel topic " * 8)
    root = store.message("general", "owner", "A message with a thread")
    store.message("general", "claude", "Thread reply", thread_id=root)
    store.close()
    screen = RoomsScreen(tmp_path)
    app = LayoutHost(style)
    async with app.run_test(size=SIZES[0]) as pilot:
        await app.push_screen(screen)
        app.apply_style()
        await screen.switch("general", root)
        screen.query_one("#room-composer", PromptInput).value = "Keep this\nmultiline\ndraft"
        await screen.message_action(f"reply-{root}")
        screen.status("A visible status message")
        for theme in THEMES:
            app.theme = theme
            for size in SIZES:
                await _resize_terminal(pilot, *size)
                assert_buttons_fit(screen.query_one("#rooms-bar"))
                assert_buttons_fit(screen.query_one("#room-toolbar"))
                assert screen.query_one("#room-transcript").region.height > 0
                assert screen.query_one("#room-compose-row").region.bottom <= app.size.height
        screen.action_sidebar()
        await pilot.pause()
        await pilot.click("#room-members-toggle")
        await app.workers.wait_for_complete()
        assert not screen.query_one("#room-sidebar").display
        assert screen.query_one("#room-main").region.width > 0
        assert_buttons_fit(screen.query_one("#room-toolbar"))
        assert screen.query_one("#room-compose-row").region.bottom <= app.size.height
        menu = RoomSettingsModal(await screen.runtime.db("room", "general"))
        await app.push_screen(menu)
        app.apply_style()
        await pilot.pause()
        clear = menu.query_one("#channel-clear", Button)
        clear.focus()
        await pilot.pause()
        assert clear.content_region.height >= 1
        assert clear.content_region.width >= 1
        await pilot.press("escape")
        await screen.action_sessions()
