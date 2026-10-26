"""The supplied Matrix3 animation stays inside Classic's banner and TUI lifecycle."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.widgets import Collapsible, Static, Switch

from novacode_cli import brand
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.matrix3 import Matrix3
from novacode_cli.tui.screens import ThemeScreen
from novacode_cli.tui.widgets import ChatMessage, MatrixRain, PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, _resize_terminal, no_system_sampling  # noqa: F401


@pytest.mark.parametrize("style", ["classic", "signal"])
@pytest.mark.timeout(30)
async def test_hidden_banner_keeps_its_last_measured_canvas(style: str):
    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        rain = app._home_banner
        measured = (rain._col_count, rain._row_count)
        app._set_home_visible(visible=False)
        await pilot.pause()
        rain.reflow(rain._art_source, 120, viewport_height=37)
        assert (rain._col_count, rain._row_count) == measured
        app._show_home_banner()
        await pilot.pause()
        assert (rain._col_count, rain._row_count) == measured

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("size", [(180, 45), (120, 38), (80, 25)])
async def test_signal_chat_and_teammate_updates_use_full_transcript_width(size: tuple[int, int]):
    NovaConfig().set("home_style", "signal")
    app = _app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app._set_home_visible(visible=False)
        user = ChatMessage(Text("You"), "user")
        user.update_body("Hello")
        answer = ChatMessage(Text("Nova Agent"), "nova")
        answer.update_body("Hello.")
        update = Collapsible(
            Static("Browser opened."),
            title="Iris · Completed · Open the Browser",
            classes="tool teammate-update",
        )
        transcript = app._transcript()
        await transcript.mount(update, user, answer)
        await pilot.pause()
        for widget in (update, user, answer):
            assert widget.region.x == transcript.content_region.x
            assert widget.region.width == transcript.scrollable_content_region.width


def test_matrix3_source_cadence_is_independent_of_render_rate():
    original = Matrix3(80, 25)
    states = []
    for fps in (15, 30, 60):
        engine = deepcopy(original)
        lines = [[" "] * 80 for _ in range(25)]
        styles = [[None] * 80 for _ in range(25)]
        palette = tuple(Style(color="white") for _ in range(17))
        for _ in range(fps):
            engine.draw(lines, styles, palette, fps)
        states.append((engine._columns, lines))
    assert states[0] == states[1] == states[2]
    assert len(original._columns) == 36
    assert any(state[0] != original._columns for state in states)


def test_japanese_rain_fits_terminal_cells_and_leaves_no_ghosts():
    engine = Matrix3(81, 20)
    for column in engine._columns:
        column.head = 10
    engine._paint_chains()
    rendered = ["".join(row) for row in engine._glyphs]
    assert all(cell_len(row) == 81 for row in rendered)
    assert set("".join(rendered)) - {" "} == set("生涯最愛の人")
    # No chains are currently onscreen; departed glyphs must disappear.
    for column in engine._columns:
        column.head = -20
    engine._step()
    assert all(set(row) == {" "} for row in engine._glyphs)
    assert all(set(row) == {-1} for row in engine._levels)


@pytest.mark.timeout(40)
async def test_signal_classic_banner_motion_controls_input_and_resize(  # noqa: PLR0915 -- one UI lifecycle.
    tmp_path: Path,
):
    NovaConfig().set("home_style", "signal")
    app = _app()
    async with app.run_test(size=(200, 42)) as pilot:
        await pilot.pause()
        assert app.query_one("#prompt-dock").region.width == app.size.width
        rain = app._home_banner
        assert isinstance(rain, MatrixRain)
        assert rain._matrix3
        assert app.theme == "nova-signal"
        assert app.screen.has_class("classic-home")
        assert not app.query_one("#home-style-header").display
        assert rain._timer is not None
        assert app._status_timer._interval == pytest.approx(0.5)
        assert brand.portrait()[0] in rain._art_source
        assert brand.WORDMARK_LINES[0] in rain._art_source
        rain.set_animation_enabled(False)
        before = rain.render().plain
        for _ in range(5):
            rain._tick()
        assert rain.render().plain != before
        rows = rain._frame_lines
        assert all(cell_len(row) == rain._col_count for row in rain.render().plain.splitlines())
        assert all(
            cell_len(rain.render_line(y).text) == rain._col_count for y in range(rain._row_count)
        )
        for y, line in enumerate(rain._art_lines):
            for x, glyph in enumerate(line):
                if glyph != " ":
                    assert rows[rain._art_top + y][rain._art_left + x] == glyph
        (tmp_path / "signal-home.svg").write_text(app.export_screenshot(), encoding="utf-8")
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.click("#prompt")
        await pilot.press("q")
        assert prompt.value == "q"  # The standalone script's quit key is ordinary input.
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        assert rain._timer is None
        screen.query_one("#home-animations-toggle", Switch).value = False
        await pilot.pause()
        screen.dismiss()
        await pilot.pause()
        assert rain._timer is None
        app._set_home_animations_enabled(enabled=True)
        assert rain._timer is not None
        app._os_focused = False
        app._paint_home_scene()
        assert rain._timer is None
        app._os_focused = True
        app._low_resource_mode = True
        app._paint_home_scene()
        assert rain._timer is None
        app._low_resource_mode = False
        app._paint_home_scene()
        assert rain._timer is not None
        prompt.value = "Keep this\nmultiline draft"
        for width, height in ((80, 25), (48, 18)):
            await _resize_terminal(pilot, width, height)
            await pilot.pause()
            assert rain._col_count == rain.size.width
            assert app.query_one("#prompt-dock").region.width == width
            assert app.query_one("#prompt-dock").region.bottom <= height
            assert prompt.region.height >= 2
            assert prompt.value == "Keep this\nmultiline draft"
            (tmp_path / f"signal-{width}.svg").write_text(app.export_screenshot(), encoding="utf-8")
        app._set_home_visible(visible=False)
        assert rain._timer is None
        message = ChatMessage(Text("Nova"), "assistant")
        message.update_body("Parser updated. **Two tests passed.**")
        await app._transcript().mount(message)
        await pilot.pause()
        assert message.region.height > 0
        assert not app.screen.has_class("classic-home")


async def test_signal_switch_reuses_banner_and_respects_explicit_theme():
    NovaConfig().set("home_style", "classic")
    app = _app()
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        rain = app._home_banner
        app._set_home_style("signal")
        await pilot.pause()
        assert app._home_banner is rain
        assert rain._matrix3
        assert app.theme == "nova-signal"
        NovaConfig().set("theme", "textual-light")
        app.theme = "textual-light"
        await pilot.pause()
        app._set_home_style("classic")
        await pilot.pause()
        assert app._home_banner is rain
        assert not rain._matrix3
        app._set_home_style("signal")
        await pilot.pause()
        assert app.theme == "textual-light"
        assert rain._matrix3
        for theme in ("ansi-dark", "ansi-light"):
            app.theme = theme
            await pilot.pause()
            rain.redraw_frame()
            assert rain.render().plain
