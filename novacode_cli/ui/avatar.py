"""Nova's avatar sprite: glyphs and colours, without a terminal.

The Textual widget and the socket client draw the same frames, so the
pixel-to-glyph rules live here and nowhere else. Textual keeps the Rich style
conversion and its inline-image path; the port ships `payload()` over the wire.

Each fallback cell covers 2x4 of the sheet's pixels, so 32x32 becomes 16x8
characters. Where the terminal has octant glyphs (Unicode 16 "BLOCK OCTANT"), a
cell shows all eight pixels as a two-colour octant; elsewhere standard quadrant
glyphs keep left/right detail with two vertical samples per cell.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.ui.avatar_frames import COUNTS, FRAME_SIZE

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ATLAS",
    "COUNTS",
    "FPS",
    "FRAME_SIZE",
    "HEIGHT",
    "NATIVE_SHEET",
    "OCTANTS",
    "REACTION_SECONDS",
    "STATES",
    "WIDTH",
    "Cell",
    "frame_pixels",
    "payload",
    "sprite",
]

#: Animation frames per second for each state.
FPS = {
    "idle": 2.0,
    "walking": 3.0,
    "running": 4.0,
    "sitting": 2.0,
    "sleeping": 0.5,
    "cursor": 2.0,
    "happy": 2.0,
    "confused": 1.5,
    "thinking": 1.5,
    "talking": 3.0,
    "window": 2.0,
    "playful": 2.0,
}
#: Fixed overlay bounds shared by every animation.
WIDTH = FRAME_SIZE[0] // 2
HEIGHT = FRAME_SIZE[1] // 4
RUNS_PER_ROW = (WIDTH + 1) // 2
#: How long the end-of-turn reaction plays before going back to idle.
REACTION_SECONDS = 4.0
#: The states in sheet order, which is the order `COUNTS` lists.
STATES = tuple(COUNTS)

RGB = tuple[int, int, int]
#: One cell: its glyph, foreground hex and background hex ("" means none).
Cell = tuple[str, str, str]

ATLAS = Path(__file__).resolve().parents[1] / "assets" / "nova-avatar.png"
NATIVE_SHEET = ATLAS.with_name("nova-avatar-hq.png")

# Octant glyph per bitmask; bit k is pixel k of the cell, row-major (0 1 / 2 3 /
# 4 5 / 6 7). U+1CD00.. holds the patterns in mask order, skipping the 26 that
# already had a character. The four single-corner ones exist only in the newer
# U+1CEAx supplement, which fewer fonts carry, so the enclosing quadrant
# stands in (a one-pixel error).
_TL, _TR, _BL, _BR = 0b00000101, 0b00001010, 0b01010000, 0b10100000
_EXISTING = {
    0: " ",
    _TL | _TR | _BL | _BR: "█",
    _TL | _TR: "▀",
    _BL | _BR: "▄",
    _TL | _BL: "▌",
    _TR | _BR: "▐",
    _TL: "▘",
    _TR: "▝",
    _BL: "▖",
    _BR: "▗",
    _TL | _BR: "▚",
    _TR | _BL: "▞",
    _TL | _BL | _BR: "▙",
    _TL | _TR | _BL: "▛",
    _TL | _TR | _BR: "▜",
    _TR | _BL | _BR: "▟",
    0b00000011: "\U0001fb82",  # upper one quarter
    0b11000000: "▂",
    0b00111111: "\U0001fb85",  # upper three quarters
    0b11111100: "▆",
    0b00010100: "\U0001fbe6",  # middle left one quarter
    0b00101000: "\U0001fbe7",  # middle right one quarter
    0b00000001: "▘",
    0b00000010: "▝",
    0b01000000: "▖",
    0b10000000: "▗",
}


def _octants() -> tuple[str, ...]:
    out, nxt = [], 0x1CD00
    for mask in range(256):
        if mask in _EXISTING:
            out.append(_EXISTING[mask])
        else:
            out.append(chr(nxt))
            nxt += 1
    return tuple(out)


OCTANTS = _octants()


@cache
def _atlas() -> object:
    """The sprite sheet as RGBA pixels, opened only when a frame is drawn."""
    from PIL import Image

    with Image.open(ATLAS) as image:
        return image.convert("RGBA")


@cache
def frame_pixels(state: str, index: int) -> tuple[tuple[RGB | None, ...], ...]:
    """One frame's pixel rows; ``None`` is transparent."""
    w, h = FRAME_SIZE
    x, y = index * w, STATES.index(state) * h
    data = _atlas().crop((x, y, x + w, y + h)).tobytes()  # RGBA, row-major
    px = [
        None if data[i + 3] == 0 else (data[i], data[i + 1], data[i + 2])
        for i in range(0, len(data), 4)
    ]
    return tuple(tuple(px[row * w : (row + 1) * w]) for row in range(h))


def _representative(colours: list[RGB]) -> str:
    """Keep an original pixel color instead of creating a blurred blend."""
    n = len(colours)
    center = (
        sum(c[0] for c in colours) // n,
        sum(c[1] for c in colours) // n,
        sum(c[2] for c in colours) // n,
    )
    r, g, b = min(colours, key=lambda color: _dist(color, center))
    return f"#{r:02x}{g:02x}{b:02x}"


def _dist(a: RGB, b: RGB) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def cell(px: Sequence[RGB | None]) -> Cell:
    """Eight pixels -> one glyph in at most two colours."""
    solid = [i for i, p in enumerate(px) if p is not None]
    if not solid:
        return " ", "", ""
    if len(solid) < len(px):
        # Transparent pixels show the background; the rest share one colour.
        mask = sum(1 << i for i in solid)
        return OCTANTS[mask], _representative([px[i] for i in solid]), ""  # type: ignore[misc]
    full: list[RGB] = list(px)  # type: ignore[arg-type]
    # Keep the most different original colours so outlines retain their contrast.
    a, b = max(((p, q) for p in full for q in full), key=lambda pq: _dist(*pq))
    near_a = [i for i, p in enumerate(full) if _dist(p, a) <= _dist(p, b)]
    mask = sum(1 << i for i in near_a)
    fg = _representative([a])
    if mask == 0xFF:  # noqa: PLR2004 — every pixel is one colour
        return "█", fg, ""
    return OCTANTS[mask], fg, _representative([b])


def quadrant_cell(px: Sequence[RGB | None]) -> Cell:
    """Keep both pixel columns using the widely supported 2x2 quadrant glyphs."""
    samples: list[RGB | None] = []
    for top in (0, 4):
        pair = []
        for column in (0, 1):
            solid = [p for p in (px[top + column], px[top + column + 2]) if p is not None]
            pair.append(
                tuple(sum(p[c] for p in solid) // len(solid) for c in range(3)) if solid else None
            )
        # Duplicate each sample vertically so the octant mask uses only quadrants.
        samples.extend(pair * 2)
    return cell(samples)


def sprite(state: str, index: int, *, octants: bool = True) -> tuple[Cell, ...]:
    """One compact frame as flat rows of cells, with stable bounds.

    The sheet's rows are four pixels tall, so every four pixel rows become one
    character row; a shorter sheet is anchored at the bottom of the sprite.
    """
    rows = frame_pixels(state, index)
    cols = len(rows[0]) // 2
    left = (WIDTH - cols) // 2
    convert = cell if octants else quadrant_cell
    cells: list[Cell] = []
    blank: Cell = (" ", "", "")
    for _ in range(HEIGHT - len(rows) // 4):
        cells.extend([blank] * WIDTH)
    for y in range(0, len(rows), 4):
        cells.extend([blank] * left)
        cells.extend(
            convert([rows[y + r][x + c] for r in range(4) for c in range(2)])
            for x in range(0, 2 * cols, 2)
        )
        cells.extend([blank] * (WIDTH - left - cols))
    return tuple(cells)


def payload() -> dict[str, object]:
    """Every animation frame as one wire value the client can play locally.

    Glyphs travel as strings and colours as two parallel palettes indexed per
    cell, so 24 frames cost a few kilobytes instead of a colour string per cell.
    """
    palette: list[tuple[str, str]] = [("", "")]
    indexes: dict[tuple[str, str], int] = {("", ""): 0}
    frames = []
    for state in STATES:
        for index in range(COUNTS[state]):
            cells = sprite(state, index)
            rows = [
                "".join(c[0] for c in cells[start : start + WIDTH])
                for start in range(0, len(cells), WIDTH)
            ]
            colors = []
            for start in range(0, len(cells), WIDTH):
                row = []
                for glyph, fg, bg in cells[start : start + WIDTH]:
                    key = ("", "") if glyph == " " else (fg, bg)
                    if key not in indexes:
                        indexes[key] = len(palette)
                        palette.append(key)
                    row.append(indexes[key])
                colors.append(row)
            frames.append({"state": state, "rows": rows, "cells": colors})
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "fps": dict(FPS),
        "foreground": [pair[0] for pair in palette],
        "background": [pair[1] for pair in palette],
        "frames": frames,
    }
