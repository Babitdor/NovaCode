"""Interface presets preserve real composer, theme, modes, and modal controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.text import Text
from textual.widgets import Button, Collapsible, Static

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.home import CONVERSATION_STYLES
from novacode_cli.tui.screens import ThemeScreen
from novacode_cli.tui.widgets import ChatMessage, PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, _resize_terminal, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

# Signal uses Classic's full-height banner, covered in test_matrix3.py.
STYLES = sorted(CONVERSATION_STYLES - {"banner", "minimal", "signal"})


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.timeout(30)
async def test_preset_chat_modes_dialog_and_small_terminal(  # noqa: PLR0915 -- one UI lifecycle.
    style: str, tmp_path: Path
) -> None:
    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        assert app.screen.has_class(f"{style}-home")
        assert app._home_banner is None
        assert "test-model" in str(app.query_one("#home-style-details", Static).render())
        prompt = app.query_one("#prompt", PromptInput)
        actions = list(app.query_one("#home-style-actions").query(Button))
        assert len(actions) == 4
        header = app.query_one("#home-style-header")
        assert 0 <= header.region.x < header.region.right <= app.size.width
        launchpad = app.query_one("#home-style-actions")
        assert all(action.region.bottom <= launchpad.region.bottom for action in actions)
        assert all(action.region.height > 0 for action in actions)
        actions[-1].focus()
        await pilot.press("enter")
        assert prompt.value == "Investigate and fix this issue: "
        assert prompt.has_focus
        (tmp_path / f"{style}-home.svg").write_text(app.export_screenshot(), encoding="utf-8")
        prompt.value = "Review the parser\nand keep this draft."
        app._set_home_visible(visible=False)
        message = ChatMessage(Text("Nova"), "assistant")
        message.update_body("Parser updated. **Two tests passed.**\n\nReady for the next change.")
        tool = Collapsible(Static("2 tests passed."), title="pytest", classes="tool")
        await app._transcript().mount(message, tool)
        await pilot.pause()
        assert message.region.height > 0
        assert tool.region.height > 0
        assert app.query_one("#workspace-layout").region.height > 0
        assert app.query_one("#prompt-dock").region.bottom <= app.size.height
        (tmp_path / f"{style}-chat.svg").write_text(app.export_screenshot(), encoding="utf-8")

        app.session_state.plan_mode_enabled = True
        app._update_mode_badge()
        await pilot.pause()
        assert prompt.has_class("plan-mode")
        assert app.query_one("#mode-badge").region.height > 0
        app.session_state.plan_mode_enabled = False
        app._update_mode_badge()

        before = app.screen.styles.background
        app.theme = "textual-light"
        await pilot.pause()
        assert app.screen.styles.background != before
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        assert screen.has_class(f"{style}-ui")
        await pilot.press("escape")
        assert app.screen is app._root_screen

        await _resize_terminal(pilot, 48, 18)
        await pilot.pause()
        dock = app.query_one("#prompt-dock")
        assert 0 <= dock.region.x < dock.region.right <= app.size.width
        assert dock.region.bottom <= app.size.height
        assert prompt.region.height >= 2
        assert prompt.value == "Review the parser\nand keep this draft."
        assert app.query_one("#workspace-layout").region.height > 0
        (tmp_path / f"{style}-small.svg").write_text(app.export_screenshot(), encoding="utf-8")
        app._set_home_visible(visible=True)
        await pilot.pause()
        assert app.screen.has_class(f"{style}-home")
        assert prompt.region.bottom <= app.size.height
        assert not app.query_one("#home-style-actions").display
