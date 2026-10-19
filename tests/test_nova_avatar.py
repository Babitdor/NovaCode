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
    frame_pixels,
    render,
)
from novacode_cli.tui.avatar_frames import COUNTS, FRAME_SIZE
from tests.test_tui_sessions import _app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novacode_cli.config import config
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", AsyncMock())


def test_octant_table_covers_every_mask_once():
    # 230 Unicode octants in mask order, the rest pre-existing block glyphs.
    assert len(OCTANTS) == 256
    octants = [c for c in OCTANTS if 0x1CD00 <= ord(c) <= 0x1CDFF]
    assert octants == [chr(0x1CD00 + i) for i in range(230)]
    assert OCTANTS[0b00000100] == "\U0001cd00"  # BLOCK OCTANT-3
    assert OCTANTS[0b11111111] == "█"
    assert OCTANTS[0b00001111] == "▀"


@pytest.mark.parametrize("glyphs", ["octants", "half-blocks"])
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
    assert sum(COUNTS.values()) == 91
    assert (WIDTH * 2, HEIGHT * 4) == FRAME_SIZE
    assert WIDTH <= 32
    assert HEIGHT <= 16


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
