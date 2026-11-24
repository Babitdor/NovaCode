"""Composer toggles keep their theme colors when hovered or keyboard focused."""

import pytest
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal
from textual.widgets import Button

from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.themes import NOVA_EXTRA_THEMES


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["nova-classic", "nova-synthwave", "textual-light"])
async def test_mode_toggle_hover_and_focus_use_readable_theme_colors(theme: str):
    ids = ("prime", "passing", "plan", "caveman", "code-mode", "oracle")

    class Toggles(App):
        CSS = NovaApp.CSS

        def compose(self) -> ComposeResult:
            with Horizontal(id="composer-actions"):
                for name in ids:
                    yield Button(f"{name}: on", id=f"{name}-toggle", classes="enabled")

    app = Toggles()
    for palette in NOVA_EXTRA_THEMES:
        app.register_theme(palette)
    app.theme = theme
    async with app.run_test(size=(140, 20)) as pilot:
        variables = app.get_css_variables()
        accent = Color.parse(variables["accent"])
        for name in ids:
            button = app.query_one(f"#{name}-toggle", Button)
            for enabled in (True, False):
                button.set_class(enabled, "enabled")
                app.set_focus(None)
                await pilot.hover(button)
                await pilot.pause()
                assert button.mouse_hover
                assert button.styles.background == accent.with_alpha(0.32)
                assert button.styles.color == accent
                await pilot.hover(offset=(0, 0))
                button.focus()
                await pilot.pause()
                assert button.has_focus
                assert button.styles.background == accent.with_alpha(0.32)
                assert button.styles.color == accent
