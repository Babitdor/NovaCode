"""Home layout preferences apply live and survive restart."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from textual.widgets import Select, Static

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.states.Session import SessionState
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.home import STYLED_HOME_STYLES
from novacode_cli.tui.screens import ThemeScreen
from novacode_cli.tui.widgets import MatrixRain
from novacode_cli.ui.ui_elements import TokenTracker
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_sessions import _FakeAgent

if TYPE_CHECKING:
    from textual.pilot import Pilot


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


async def _resize_terminal(pilot: Pilot, width: int, height: int) -> None:
    """Wait for Textual's throttled resize to reach the rendered screen.

    App.size changes before the resize timer fires, so draining message queues
    alone can leave Windows CI inspecting geometry from the previous viewport.
    """
    from textual.geometry import Size

    await pilot.resize_terminal(width, height)
    async with asyncio.timeout(5):
        while pilot.app.screen._compositor.size != Size(width, height):
            await pilot.pause(0.01)
    await pilot.pause()


@pytest.mark.timeout(30)
async def test_resize_waits_for_screen_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.geometry import Size

    app = _app()
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        original_timer = app.set_timer

        def delayed_timer(delay: float, callback: object, **kwargs: object) -> object:
            return original_timer(0.25 if delay == 1 / 120 else delay, callback, **kwargs)

        monkeypatch.setattr(app, "set_timer", delayed_timer)
        await _resize_terminal(pilot, 48, 18)
        assert app.screen.size == Size(48, 18)
        assert app.query_one("#prompt-dock").region.right <= 48


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
            assert app.query_one("#home-header").region.width == app.size.width


async def test_settings_switches_home_live_and_persists() -> None:
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        root = app.screen
        banner = app._home_banner
        marker = Static("Keep this transcript entry")
        await app._transcript().mount(marker)
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        selector = screen.query_one("#home-style-select", Select)
        assert selector.value == "dashboard"
        for enabled in (False, True, False):
            style = "dashboard" if enabled else "classic"
            selector.value = style
            await pilot.pause()
            assert root.has_class("home") is enabled
            assert root.has_class("classic-home") is not enabled
            assert not screen.has_class("home")
            assert not screen.has_class("classic-home")
            assert marker.is_mounted
            assert app._home_banner is banner
            assert len(app.query(MatrixRain)) == 1
            assert banner._compact is enabled
            assert NovaConfig().get("home_style") == style
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
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#home-style-select", Select).value = "classic"
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
        assert app.query_one("#home-header").size.height > 0, "a short window keeps NOVA"
        await pilot.resize_terminal(70, 16)
        await pilot.pause()
        assert app.query_one("#home-header").size.height == 0
        assert app.query_one("#prompt").size.height > 0


async def test_late_system_sample_after_panel_removal_is_ignored() -> None:
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        await app.query_one("#home-system").remove()
        app._show_home_system(None)


def test_style_validation_preserves_legacy_choice_and_prefers_explicit_style() -> None:
    from novacode_cli.tui.home import HOME_STYLES, home_style

    for _, style in HOME_STYLES:
        assert home_style(style, legacy_dashboard=False) == style
    for invalid in (None, "unknown", False, []):
        assert home_style(invalid) == "dashboard"
        assert home_style(invalid, legacy_dashboard=False) == "classic"


@pytest.mark.parametrize("style", ["centered", "banner", "minimal"])
async def test_reference_layouts_use_the_real_composer_and_leave_room_for_it(
    style: str,
    no_system_sampling: Mock,
) -> None:
    from novacode_cli.tui.widgets import PromptInput

    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        assert app.screen.has_class(f"{style}-home")
        assert app._home_banner is None
        no_system_sampling.assert_not_called()
        assert app.query_one("#home-header").size.height == 0
        assert app.query_one("#home-left").size.width == 0
        assert app.query_one("#home-right").size.width == 0
        header = app.query_one("#home-style-header")
        dock = app.query_one("#prompt-dock")
        assert header.size.height > 0
        assert app.query_one("#prompt").size.height > 0
        assert dock.region.bottom <= app.size.height
        if style == "centered":
            assert dock.size.width == 76
            assert header.region.x == dock.region.x
            assert abs((header.region.y + dock.region.bottom) / 2 - app.size.height / 2) <= 2
        else:
            assert header.region.y <= 2
            assert dock.region.y >= app.size.height - 10
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "First line\nSecond line"
        await pilot.pause()
        assert prompt.size.height >= 2
        assert prompt.soft_wrap
        assert "test-model" in str(app.query_one("#home-composer-meta").render())
        await pilot.resize_terminal(48, 16)
        await pilot.pause()
        assert dock.region.x >= 0
        assert dock.region.right <= app.size.width
        assert dock.region.bottom <= app.size.height
        assert prompt.size.height >= 2
        assert prompt.value == "First line\nSecond line"


@pytest.mark.timeout(40)
async def test_style_selector_preserves_draft_and_allows_keyboard_switching() -> None:
    from novacode_cli.tui.home import HOME_STYLES
    from novacode_cli.tui.widgets import PromptInput

    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        root = app.screen
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "Keep this\nmultiline draft"
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        selector = screen.query_one("#home-style-select", Select)
        for _, style in HOME_STYLES:
            selector.value = style
            await pilot.pause()
            assert app._home_style == style
            assert prompt.value == "Keep this\nmultiline draft"
            assert app.query_one("#prompt", PromptInput) is prompt
            assert NovaConfig().get("home_style") == style
            assert not screen.has_class("styled-home")
            assert root.has_class("styled-home") is (style in STYLED_HOME_STYLES)
        selector.value = "classic"
        selector.focus()
        await pilot.press("enter", "down", "enter")
        await pilot.pause()
        assert selector.value == "centered"
        assert NovaConfig().get("home_style") == "centered"
        screen.dismiss()
        await pilot.pause()
        assert app.screen.has_class("centered-home")

    restarted = _app()
    async with restarted.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        assert restarted._home_style == "centered"
        assert restarted.screen.has_class("centered-home")


async def test_centered_completion_palette_stays_with_the_composer() -> None:
    from textual.widgets import OptionList

    from novacode_cli.tui.widgets import PromptInput

    NovaConfig().set("home_style", "centered")
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", PromptInput)
        prompt.focus()
        await pilot.press("/")
        await pilot.pause()
        palette = app.query_one("#cmdpalette", OptionList)
        dock = app.query_one("#prompt-dock")
        assert palette.display
        assert palette.region.x == dock.region.x
        assert palette.region.right == dock.region.right
        assert palette.region.bottom <= dock.region.y
        assert dock.region.bottom <= app.size.height


async def test_reference_layouts_recolor_with_the_active_theme() -> None:
    from textual.color import Color
    from textual.content import Content
    from textual.style import Style

    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        for style in ("centered", "banner", "minimal"):
            app._set_home_style(style)
            for theme in ("flexoki", "matrix"):
                app.theme = theme
                await pilot.pause()
                art = app.query_one("#home-style-art", Static).render()
                assert isinstance(art, Content)
                colors = [
                    (
                        Style.parse(span.style) if isinstance(span.style, str) else span.style
                    ).foreground
                    for span in art.spans
                ]
                expected = app._palette.accent if style == "banner" else app._palette.text
                assert Color.parse(expected) in colors


@pytest.mark.parametrize("style", ["centered", "banner", "minimal"])
async def test_new_styles_yield_to_chat_and_return_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    style: str,
) -> None:
    from unittest.mock import AsyncMock

    from novacode_cli.tui.widgets import PromptInput

    monkeypatch.setattr("novacode_cli.agent_stream._maybe_verifier", lambda: None)
    NovaConfig().set("home_style", style)
    app = _app()
    monkeypatch.setattr(app, "_consolidate_learning", AsyncMock())
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "Hello"
        prompt.focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app._home_visible
        assert not app.screen.has_class("styled-home")
        assert app.query_one("#home-style-header").size.height == 0
        assert app.query_one("#workspace-layout").size.height > 0
        assert app.query_one("#prompt-dock").styles.dock == "bottom"
        full_style = style in {"banner", "minimal"}
        assert app.screen.has_class("conversation-style") is full_style
        if full_style:
            assert app.screen.has_class(f"{style}-ui")
            assert app.query_one("#conversation-style-heading").size.height == 1
            assert app.query_one("#info-bar").size.height == 1
        await app._run_clear()
        await pilot.pause()
        assert app._home_visible
        assert app.screen.has_class(f"{style}-home")
        assert app.query_one("#prompt", PromptInput) is prompt


@pytest.mark.parametrize("style", ["banner", "minimal"])
async def test_conversation_styles_keep_usage_modes_and_dialogs_usable(style: str) -> None:
    from rich.text import Text
    from textual.widgets import Collapsible

    from novacode_cli import ui_events as ev
    from novacode_cli.context import ContextBreakdown
    from novacode_cli.tui.widgets import ChatMessage, PromptInput

    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        app._set_home_visible(visible=False)
        message = ChatMessage(Text("You"), "user")
        message.update_body("Keep this conversation")
        tool = Collapsible(Static("Test output"), title="Tool result", classes="tool")
        await app._transcript().mount(message, tool)
        await pilot.pause()
        assert message.styles.background.a == 0
        assert message.styles.padding.top == 0
        assert tool.styles.padding.left == (0 if style == "banner" else 1)
        assert app.token_tracker is not None
        app.token_tracker.set_breakdown(
            ContextBreakdown(
                total_tokens=1000,
                context_window_size=10000,
            )
        )
        await app._render(ev.ContextUsage(input_tokens=2000))
        assert "20%" in str(app.query_one("#prompt-hint-bar").render())
        assert "ctx 20%" in str(app.query_one("#info-quota").render())
        assert app.query_one("#status-row").size.height > 0
        app.session_state.plan_mode_enabled = True
        app._update_mode_badge()
        await pilot.pause()
        assert app.query_one("#mode-badge").size.height > 0
        assert "PLAN" in str(app.query_one("#mode-badge").render())
        assert app.query_one("#prompt", PromptInput).has_class("plan-mode")
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        assert screen.has_class("conversation-style")
        assert screen.has_class(f"{style}-ui")
        screen.query_one("#home-style-select", Select).value = "classic"
        await pilot.pause()
        assert not app._root_screen.has_class("conversation-style")
        assert not screen.has_class("conversation-style")
        assert message.is_mounted
        assert message.raw_text == "Keep this conversation"
        screen.dismiss()
        await pilot.pause()
        assert app.query_one("#info-bar").size.height == 2


@pytest.mark.parametrize("style", ["banner", "minimal"])
async def test_model_modal_inherits_interface_style(
    style: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.widgets import Input, OptionList

    from novacode_cli.tui.screens import ConfirmModal, ModelScreen

    monkeypatch.setattr(ModelScreen, "_load", lambda _self: None)
    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(130, 44)) as pilot:
        await pilot.pause()
        screen = ModelScreen()
        app.push_screen(screen)
        await pilot.pause()
        assert screen.has_class(f"{style}-ui")
        box = screen.query_one("#modal-box")
        assert box.styles.border_top[0] == ("round" if style == "banner" else "")
        listing = screen.query_one("#model-options", OptionList)
        listing.add_options(["Model one", "Model two"])
        listing.highlighted = 0
        listing.focus()
        await pilot.press("down")
        await pilot.pause()
        assert listing.highlighted == 1
        assert listing.styles.border_top[0] in {"", "none"}
        assert listing.styles.background.a == 0
        assert screen.query_one("#switch").region.bottom <= app.size.height
        assert screen.query_one("#cancel").styles.border_top[0] in {"", "none"}
        assert screen.query_one("#model-filter", Input).styles.background.a == (
            0 if style == "banner" else 1
        )
        confirmation = ConfirmModal("Confirm this choice?", "Model selection")
        app.push_screen(confirmation)
        await pilot.pause()
        assert confirmation.has_class(f"{style}-ui")
        await pilot.press("escape")
        assert app.screen is screen
        await pilot.press("escape")
        assert app.screen is app._root_screen


@pytest.mark.parametrize("style", ["banner", "minimal", "dashboard"])
async def test_style_refresh_tolerates_removed_layout(style: str) -> None:
    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(130, 44)) as pilot:
        await pilot.pause()
        selector = "#home-intro" if style == "dashboard" else "#home-style-header"
        await app.query_one(selector).remove()
        app._refresh_home_dashboard()
        app._set_home_visible(visible=False)
        app._refresh_home_dashboard()


@pytest.mark.parametrize("style", ["banner", "minimal"])
async def test_style_covers_status_live_panels_and_form_controls(style: str) -> None:
    from novacode_cli import ui_events as ev
    from novacode_cli.context import ContextBreakdown
    from novacode_cli.tui.terminal import EmbeddedTerminal
    from novacode_cli.tui.widgets import QuestionDock

    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        app._set_home_visible(visible=False)
        assert app.token_tracker is not None
        app.token_tracker.set_breakdown(
            ContextBreakdown(
                total_tokens=1000,
                context_window_size=10000,
            )
        )
        await app._render(ev.ContextUsage(input_tokens=2900))
        status = str(app.query_one("#prompt-hint-bar").render())
        assert "ctx 29%" in status
        assert "┃" not in status
        assert "━" not in status
        assert "▌" not in status
        assert "ctx 29%" in str(app.query_one("#info-quota").render())
        terminal = EmbeddedTerminal()
        await app._transcript().mount(terminal)
        subagents = app.query_one("#subagents-dock")
        subagents.add_class("active")
        question = app.query_one("#question-dock", QuestionDock)
        question.activate({"question": "Choose a path", "options": ["First", "Second"]})
        await pilot.pause()
        assert subagents.styles.background.a == 0
        assert subagents.styles.border_top[0] in {"", "none"}
        assert question.styles.background.a == 0
        assert terminal.styles.background.a == 0
        assert terminal.query_one("#term-log").styles.border_top[0] in {"", "none"}
        question.deactivate()
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        switch = screen.query_one("#matrix-rain-toggle")
        assert switch.size.height == 1
        assert switch.styles.border_top[0] in {"", "none"}
        switch.focus()
        await pilot.press("space")
        await pilot.pause()
        assert NovaConfig().get("matrix_rain_enabled") is True
        assert switch.styles.border_top[0] in {"", "none"}
        assert screen.query_one("#close").styles.border_top[0] in {"", "none"}
        screen.dismiss()
        await pilot.pause()
        app._set_home_style("classic")
        app._refresh_usage_metrics()
        assert "CTX" in str(app.query_one("#prompt-hint-bar").render())
