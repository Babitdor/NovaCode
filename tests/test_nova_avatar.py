"""/nova pixel avatar: octant frames and the toggle beside the prompt."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.tui.avatar import (
    FPS,
    HEIGHT,
    OCTANTS,
    WIDTH,
    NovaAvatar,
    _dist,
    _octant_cell,
    _quadrant_cell,
    frame_pixels,
    native_frame,
    native_image_type,
    render,
)
from novacode_cli.tui.avatar_frames import COUNTS, FRAME_SIZE
from tests.test_tui_sessions import _app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novacode_cli.config import config
    from novacode_cli.tui import avatar as avatar_module
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", AsyncMock())
    monkeypatch.setattr(avatar_module, "native_image_type", lambda: None)


def test_octant_table_covers_every_mask_once():
    # 230 Unicode octants in mask order, the rest pre-existing block glyphs.
    assert len(OCTANTS) == 256
    octants = [c for c in OCTANTS if 0x1CD00 <= ord(c) <= 0x1CDFF]
    assert octants == [chr(0x1CD00 + i) for i in range(230)]
    assert OCTANTS[0b00000100] == "\U0001cd00"  # BLOCK OCTANT-3
    assert OCTANTS[0b11111111] == "█"
    assert OCTANTS[0b00001111] == "▀"


@pytest.mark.parametrize("glyphs", ["octants", "quadrants"])
def test_every_frame_renders_to_a_fixed_width(glyphs: str):
    assert set(COUNTS) == set(FPS)
    for state, count in COUNTS.items():
        for i in range(count):
            rows = frame_pixels(state, i)
            assert (len(rows[0]), len(rows)) == FRAME_SIZE
            assert any(p is not None for row in rows for p in row), (state, i)
            lines = render(state, i, octants=glyphs == "octants").plain.split("\n")
            assert len(lines) == HEIGHT
            assert {len(line) for line in lines} == {WIDTH}


def test_two_colour_cell_keeps_the_split():
    # Top half one colour, bottom half another: exactly an upper half block.
    px = [(200, 40, 40)] * 4 + [(40, 40, 200)] * 4
    glyph, style = _octant_cell(px)
    assert glyph in {"▀", "▄"}
    assert style.bgcolor is not None


def test_fallback_preserves_left_right_detail_and_transparency():
    ink = (80, 40, 20)
    glyph, style = _quadrant_cell([ink, None] * 4)
    assert glyph == "▌"
    assert style.bgcolor is None
    assert style.color.get_truecolor() == ink
    assert _quadrant_cell([None] * 8) == (" ", None)
    for mask in range(256):
        glyph, _ = _quadrant_cell([ink if mask & (1 << i) else None for i in range(8)])
        assert glyph in " ▀▄█▌▐▘▝▖▗▚▞▙▛▜▟"


def test_fallback_retains_two_color_vertical_edges():
    dark, light = (20, 10, 5), (240, 180, 130)
    glyph, style = _quadrant_cell([dark, light] * 4)
    assert glyph in "▌▐"
    assert {style.color.get_truecolor(), style.bgcolor.get_truecolor()} == {dark, light}


@pytest.mark.parametrize("design", ["classic", "dashboard", "minimal"])
async def test_nova_toggles_the_avatar_and_remembers_it_without_resizing_content(design: str):
    from novacode_cli.config.nova_config import NovaConfig

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.screen.add_class(f"{design}-ui")
        app._set_home_visible(visible=False)
        await pilot.pause()
        avatar = app.query_one("#nova-avatar", NovaAvatar)
        assert not avatar.display
        transcript_region = app.query_one("#transcript").region
        prompt_region = app.query_one("#prompt-dock").region
        app._run_nova()
        await pilot.pause()
        assert avatar.display
        assert NovaConfig().get("nova_avatar") is True
        # The sprite floats over content; it reserves neither rows nor columns.
        prompt = app.query_one("#prompt-dock")
        assert app.query_one("#transcript").region == transcript_region
        assert prompt.region == prompt_region
        assert app.screen.region.contains_region(avatar.sprite_region)
        assert avatar.sprite_region.bottom <= prompt.region.y
        assert avatar.sprite_region.overlaps(transcript_region)
        occupied = [row for row in avatar._rows if row.display]
        assert occupied
        assert all(row.visible and row.region.width > 0 for row in occupied)
        # Transparent cells belong to the conversation underneath, not a panel.
        state, frame = avatar._shown
        lines = render(state, frame, octants=avatar._octants).plain.split("\n")
        for y, line in enumerate(lines):
            for x, glyph in enumerate(line):
                if glyph == " ":
                    widget, _ = app.screen.get_widget_at(
                        avatar.sprite_region.x + x, avatar.sprite_region.y + y
                    )
                    assert widget is not avatar
                    assert widget not in avatar._rows
        row = occupied[0]
        widget, _ = app.screen.get_widget_at(row.region.x, row.region.y)
        assert widget is row
        app._turn_active = True
        app._paint_avatar()
        assert avatar._shown[0] == "thinking"
        app._turn_active = False
        app._run_nova()
        await pilot.pause()
        assert not avatar.display
    assert _app()._avatar_on is False


def test_all_sheet_animations_fit_the_smaller_overlay():
    assert set(COUNTS) == {
        "idle",
        "walking",
        "running",
        "sitting",
        "sleeping",
        "cursor",
        "happy",
        "confused",
        "thinking",
        "talking",
        "window",
        "playful",
    }
    assert set(COUNTS.values()) == {2}
    assert (WIDTH * 2, HEIGHT * 4) == FRAME_SIZE
    assert WIDTH == 16
    assert HEIGHT == 8


def test_color_reduction_preserves_crisp_original_colors():
    from novacode_cli.tui.avatar import _representative

    assert _representative([(255, 0, 0), (0, 0, 255)]) in {"#ff0000", "#0000ff"}


def test_octant_colors_preserve_the_strongest_edges():
    black, white, gray = (0, 0, 0), (255, 255, 255), (80, 80, 80)
    _, style = _octant_cell([black, white, gray, gray, gray, gray, gray, gray])
    assert style is not None
    assert style.color is not None
    assert style.bgcolor is not None
    foreground = style.color.get_truecolor()
    background = style.bgcolor.get_truecolor()
    assert _dist(foreground, background) == _dist(black, white)


async def test_every_animation_is_reachable_from_activity_or_interaction(
    monkeypatch: pytest.MonkeyPatch,
):
    from types import SimpleNamespace

    from novacode_cli.tui import avatar as avatar_module

    clock = [0.0]
    monkeypatch.setattr(avatar_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    from textual.app import App

    async with App().run_test():
        avatar = NovaAvatar()
        seen = set()
        for moment in (0.0, 9.0, 17.0, 25.0, 61.0):
            clock[0] = moment
            avatar.animate_to(busy=False, talking=False)
            seen.add(avatar._shown[0])
        for talking, working in ((False, False), (True, False), (False, True)):
            avatar.animate_to(busy=True, talking=talking, working=working)
            seen.add(avatar._shown[0])
        for reaction in ("happy", "confused", "window"):
            avatar.react(reaction)
            avatar.animate_to(busy=False, talking=False)
            seen.add(avatar._shown[0])
        avatar.on_mouse_move()
        avatar.animate_to(busy=False, talking=False)
        seen.add(avatar._shown[0])
        assert seen == set(COUNTS)


async def test_avatar_stays_hidden_on_a_small_terminal():
    app = _app()
    async with app.run_test(size=(80, 24)) as pilot:
        app._run_nova()
        await pilot.pause()
        assert not app.query_one("#nova-avatar", NovaAvatar).display


async def test_overlay_reanchors_after_resize_and_growing_composer():
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._set_home_visible(visible=False)
        app._run_nova()
        await pilot.pause()
        avatar = app.query_one("#nova-avatar", NovaAvatar)
        prompt = app.query_one("#prompt")
        prompt.text = "one\ntwo\nthree\nfour"
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        assert avatar.sprite_region.bottom < app.query_one("#prompt-dock").region.y
        await pilot.resize_terminal(100, 32)
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        assert app.screen.region.contains_region(avatar.sprite_region)
        assert avatar.sprite_region.bottom < app.query_one("#prompt-dock").region.y
        assert avatar._shown[0] == "window"


async def test_overlay_yields_to_menus_and_modals():
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ConfirmModal

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._run_nova()
        await pilot.pause()
        avatar = app.query_one("#nova-avatar", NovaAvatar)
        row = next(row for row in avatar._rows if row.display)
        await pilot.hover(row)
        app._paint_avatar()
        assert avatar._shown[0] == "cursor"
        palette = app.query_one("#cmdpalette", OptionList)
        palette.display = True
        app._paint_avatar()
        assert not avatar.display
        palette.display = False
        app.push_screen(ConfirmModal("Confirm", "Testing the overlay"))
        await pilot.pause()
        app._paint_avatar()
        assert not avatar.display
        app.pop_screen()
        await pilot.pause()
        app._paint_avatar()
        assert avatar.display


def test_native_frames_keep_source_detail_and_all_animation_pairs():
    for state, count in COUNTS.items():
        for index in range(count):
            image = native_frame(state, index)
            assert image.size == (256, 256)
            assert image.getchannel("A").getextrema()[0] == 0
            assert image.getchannel("A").getbbox()
    assert native_frame("idle", 0).size[0] > FRAME_SIZE[0] * 6


def test_graphics_probe_skips_redirected_input(monkeypatch):
    from types import SimpleNamespace

    from novacode_cli.tui import avatar as avatar_module

    native_image_type.cache_clear()
    stream = SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(avatar_module, "sys", SimpleNamespace(__stdin__=stream, __stdout__=stream))
    assert native_image_type() is None
    native_image_type.cache_clear()


@pytest.mark.parametrize("backend", ["SixelImage", "TGPImage", "HalfcellImage"])
def test_graphics_probe_selects_only_native_backends(monkeypatch, backend):
    from types import SimpleNamespace

    import textual_image.widget as images

    from novacode_cli.tui import avatar as avatar_module
    from novacode_cli.tui.avatar_images import SixelAvatarImage

    stream = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(avatar_module, "sys", SimpleNamespace(__stdin__=stream, __stdout__=stream))
    monkeypatch.setattr(images, "Image", getattr(images, backend))
    native_image_type.cache_clear()
    try:
        expected = {
            "SixelImage": SixelAvatarImage,
            "TGPImage": images.TGPImage,
            "HalfcellImage": None,
        }
        assert native_image_type() is expected[backend]
    finally:
        native_image_type.cache_clear()


async def test_inline_sixel_avatar_animates_resizes_and_yields_to_dialogs(monkeypatch):
    from novacode_cli.tui import avatar as avatar_module
    from novacode_cli.tui.avatar_images import SixelAvatarImage
    from novacode_cli.tui.screens import ConfirmModal

    monkeypatch.setattr(avatar_module, "native_image_type", lambda: SixelAvatarImage)
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._set_home_visible(visible=False)
        app._avatar_on = True
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        avatar = app.query_one("#nova-avatar", NovaAvatar)
        assert isinstance(avatar._native, SixelAvatarImage)
        assert avatar._native.image.size == (256, 256)
        assert avatar._native.region == avatar.sprite_region
        prompt = app.query_one("#prompt-dock").region
        assert avatar.sprite_region.right == min(prompt.right - 1, app.size.width - 1)
        assert avatar.sprite_region.bottom == prompt.y - 1
        assert not any(row.display for row in avatar._rows)
        from rich.control import Control
        from textual.geometry import Region

        from novacode_cli.tui.avatar_images import _AvatarSixelData

        data = avatar._native.query_one(_AvatarSixelData)
        strips = data.render_lines(Region(0, 0, WIDTH, HEIGHT))
        assert len(strips) == HEIGHT
        assert all(next(iter(strip)).text == " " * WIDTH for strip in strips)
        output = strips[-1].render(app.console)
        assert "\x1bP" in output
        assert output.endswith(
            Control.move_to(data.region.right, data.region.bottom - 1).segment.text
        )
        app._turn_active = True
        app._paint_avatar()
        await pilot.pause()
        assert avatar._shown[0] == "thinking"
        assert avatar._native.image is native_frame(*avatar._shown)
        await pilot.resize_terminal(100, 32)
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        assert avatar._native.region == avatar.sprite_region
        assert app.screen.region.contains_region(avatar.sprite_region)
        prompt = app.query_one("#prompt-dock").region
        assert avatar.sprite_region.right == min(prompt.right - 1, app.size.width - 1)
        assert avatar.sprite_region.bottom == prompt.y - 1
        data = avatar._native.query_one(_AvatarSixelData)
        output = data.render_lines(Region(0, 0, WIDTH, HEIGHT))[0].render(app.console)
        assert Control.move_to(data.region.x, data.region.y).segment.text in output
        await app.push_screen(ConfirmModal("Preview", "Dialog covers the avatar"))
        app._paint_avatar()
        await pilot.pause()
        assert not avatar.display
        assert "\x1bP" not in app.screen._compositor.render_full_update().render_segments(app.console)
        app.pop_screen()
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        assert avatar.display
        app._avatar_on = False
        app._paint_avatar()
        await pilot.pause()
        assert not avatar.display


async def test_inline_image_failure_returns_to_character_renderer(monkeypatch):
    from textual_image.widget import SixelImage

    from novacode_cli.tui import avatar as avatar_module

    monkeypatch.setattr(avatar_module, "native_image_type", lambda: SixelImage)

    def missing_frame(*args):
        raise OSError("Missing packaged image")

    monkeypatch.setattr(avatar_module, "native_frame", missing_frame)
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._set_home_visible(visible=False)
        app._avatar_on = True
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        avatar = app.query_one("#nova-avatar", NovaAvatar)
        assert avatar._native is None
        assert any(row.display for row in avatar._rows)


async def test_inline_avatar_survives_populated_chat_composition(monkeypatch):
    from textual.geometry import Region
    from textual.widgets import Static

    from novacode_cli.tui import avatar as avatar_module
    from novacode_cli.tui.avatar_images import SixelAvatarImage

    monkeypatch.setattr(avatar_module, "native_image_type", lambda: SixelAvatarImage)
    app = _app()
    async with app.run_test(size=(110, 36)) as pilot:
        app._set_home_visible(visible=False)
        await app._transcript().mount(Static("Chat message with content " * 600))
        app._transcript().scroll_end(animate=False)
        app._avatar_on = True
        await pilot.pause()
        app._paint_avatar()
        await pilot.pause()
        compositor = app.screen._compositor
        output = compositor.render_full_update().render_segments(app.console)
        assert "\x1bP" in output, "Chat layout must not discard the inline image"
        bounds = app.query_one("#nova-avatar", NovaAvatar).sprite_region
        for row in range(bounds.y, bounds.bottom):
            compositor._dirty_regions.add(Region(bounds.x + 5, row, 1, 1))
            update = compositor.render_partial_update()
            assert update is not None
            assert "\x1bP" in update.render_segments(app.console), row


def test_sixel_strips_preserve_images_and_cursor_through_cuts_and_crops():
    from rich.console import Console
    from rich.control import Control
    from rich.segment import Segment
    from textual.strip import Strip

    from novacode_cli.tui.avatar_images import _SixelStrip

    console = Console()
    sixel = "\x1bPqencoded-band\x1b\\"
    strip = _SixelStrip(Strip([Segment(" " * 16)], 16), sixel, (20, 10), 20)
    start = 20
    for part in strip.divide([3, 9, 16]):
        start += part.cell_length
        output = part.render(console)
        assert sixel in output
        assert output.endswith(Control.move_to(start, 10).segment.text)
    part = strip.crop(3, 8)
    assert part.cell_length == 5
    assert sixel in part.render(console)
    assert part.render(console).endswith(Control.move_to(28, 10).segment.text)
    assert strip.crop(3, 3).render(console) == ""
