"""Nova's transparent avatar, summoned with ``/nova``.

Frames live in ``assets/nova-avatar.png`` (built by ``scripts/build_avatar.py``;
layout in ``avatar_frames.py``). Each terminal cell covers 2x4 of their pixels.
Where the terminal has octant glyphs (Unicode 16 "BLOCK OCTANT"), a cell shows all eight pixels as a
two-colour octant; elsewhere it falls back to half-blocks, one representative 2x2
pixel per half. Animation follows what the agent is doing.
"""

from __future__ import annotations

import os
import re
import time
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.style import Style
from rich.text import Text
from textual.geometry import Region
from textual.widgets import Static

from novacode_cli.tui.avatar_frames import COUNTS, FRAME_SIZE

if TYPE_CHECKING:
    from textual.app import ComposeResult

__all__ = ["OCTANTS", "NovaAvatar", "render"]

#: Animation frames per second for each state.
FPS = {
    "idle": 2.0,
    "walking": 5.0,
    "running": 6.0,
    "sitting": 2.0,
    "sleeping": 1.2,
    "cursor": 5.0,
    "happy": 5.0,
    "confused": 4.0,
    "thinking": 3.0,
    "talking": 6.0,
    "window": 3.0,
    "playful": 3.5,
}
#: Fixed overlay bounds shared by every animation.
WIDTH = FRAME_SIZE[0] // 2
HEIGHT = FRAME_SIZE[1] // 4
RUNS_PER_ROW = (WIDTH + 1) // 2
#: How long the end-of-turn reaction plays before going back to idle.
REACTION_SECONDS = 4.0

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

RGB = tuple[int, int, int]


ATLAS = Path(__file__).resolve().parents[1] / "assets" / "nova-avatar.png"


@cache
def _atlas() -> Any:  # noqa: ANN401 — a PIL image, imported only when drawn
    from PIL import Image

    with Image.open(ATLAS) as image:
        return image.convert("RGBA")


@cache
def frame_pixels(state: str, index: int) -> tuple[tuple[RGB | None, ...], ...]:
    """One frame's pixel rows; ``None`` is transparent."""
    w, h = FRAME_SIZE
    x, y = index * w, list(COUNTS).index(state) * h
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


def _octant_cell(px: list[RGB | None]) -> tuple[str, Style | None]:
    """Eight pixels -> one glyph in at most two colours."""
    solid = [i for i, p in enumerate(px) if p is not None]
    if not solid:
        return " ", None
    if len(solid) < len(px):
        # Transparent pixels show the background; the rest share one colour.
        mask = sum(1 << i for i in solid)
        return OCTANTS[mask], Style(color=_representative([px[i] for i in solid]))
    full: list[RGB] = px  # type: ignore[assignment]
    # Keep the most different original colours so outlines retain their contrast.
    a, b = max(((p, q) for p in full for q in full), key=lambda pq: _dist(*pq))
    near_a = [i for i, p in enumerate(full) if _dist(p, a) <= _dist(p, b)]
    mask = sum(1 << i for i in near_a)
    fg = _representative([a])
    if mask == 0xFF:  # noqa: PLR2004 — every pixel is one colour
        return "█", Style(color=fg)
    bg = _representative([b])
    return OCTANTS[mask], Style(color=fg, bgcolor=bg)


def _half_cell(px: list[RGB | None]) -> tuple[str, Style | None]:
    """Eight pixels -> ``▀`` with each half the representative color of its 2x2 block."""
    halves = []
    for block in (px[:4], px[4:]):
        solid = [p for p in block if p is not None]
        halves.append(_representative(solid) if len(solid) >= 2 else None)  # noqa: PLR2004
    top, bottom = halves
    return _half_block(top, bottom)


def _half_block(top: str | None, bottom: str | None) -> tuple[str, Style | None]:
    """Preserve each half-cell's color, with transparent empty halves."""
    if top is None and bottom is None:
        return " ", None
    if top is None:
        return "▄", Style(color=bottom)
    return "▀", Style(color=top, bgcolor=bottom)


def octants_supported() -> bool:
    """Terminals known to draw octants (built-in glyphs or a bundled font)."""
    env = os.environ
    return bool(env.get("WT_SESSION")) or env.get("TERM_PROGRAM") in {"WezTerm", "ghostty"}


@cache
def render(state: str, index: int, *, octants: bool = True) -> Text:
    """One compact frame, with stable bounds and feet anchored at the bottom."""
    rows = frame_pixels(state, index)
    cols = len(rows[0]) // 2
    left = (WIDTH - cols) // 2
    cell = _octant_cell if octants else _half_cell
    text = Text(no_wrap=True)
    for _ in range(HEIGHT - len(rows) // 4):
        text.append(" " * WIDTH + "\n")
    for y in range(0, len(rows), 4):
        if y:
            text.append("\n")
        text.append(" " * left)
        for x in range(0, 2 * cols, 2):
            px = [rows[y + r][x + c] for r in range(4) for c in range(2)]
            text.append(*cell(px))
        text.append(" " * (WIDTH - left - cols))
    return text


class NovaAvatar(Static):
    """A floating sprite above the composer, outside the document flow."""

    DEFAULT_CSS = """
    NovaAvatar {
        overlay: screen;
        position: absolute;
        background: transparent;
        visibility: hidden;
        padding: 0;
        content-align: center bottom;
        display: none;
    }
    NovaAvatar > .avatar-row {
        overlay: screen; position: absolute; visibility: visible;
        height: 1; padding: 0; background: transparent;
    }
    """

    def __init__(self, **kwargs: Any) -> None:  # noqa: D107
        super().__init__("", **kwargs)
        self.styles.width = WIDTH
        self.styles.height = HEIGHT
        self._mood = "idle"
        self._mood_until = 0.0
        self._idle_since = time.monotonic()
        self._octants = octants_supported()
        self._shown: tuple[str, int] | None = None
        self._rows = [Static("", classes="avatar-row") for _ in range(HEIGHT * RUNS_PER_ROW)]

    def compose(self) -> ComposeResult:
        """Draw occupied glyph runs; the rectangular host never paints."""
        yield from self._rows

    @property
    def sprite_region(self) -> Region:
        """The logical sprite bounds, independent of its transparent host."""
        position = self.absolute_offset
        return Region(position.x, position.y, WIDTH, HEIGHT) if position else Region()

    def react(self, mood: str) -> None:
        """Play a reaction without interrupting the agent's active animation."""
        if mood not in FPS:
            return
        self._idle_since = time.monotonic()
        self._mood, self._mood_until = mood, self._idle_since + REACTION_SECONDS

    def on_mouse_move(self) -> None:
        """React when the cursor moves over the visible sprite."""
        self.react("cursor")

    def on_click(self) -> None:
        """A click wakes Nova and plays her cursor interaction."""
        self.react("cursor")

    def animate_to(self, *, busy: bool, talking: bool, working: bool = False) -> None:
        """Advance the animation; repaints only when the frame changes."""
        now = time.monotonic()
        if busy:
            self._idle_since = now
            state = "talking" if talking else ("running" if working else "thinking")
        else:
            idle_time = now - self._idle_since
            if now < self._mood_until:
                state = self._mood
            elif idle_time >= 60:  # noqa: PLR2004 — sleep after a minute of inactivity
                state = "sleeping"
            else:
                state = ("idle", "sitting", "playful", "walking")[int(idle_time // 8) % 4]
        frame = (state, int(now * FPS[state]) % COUNTS[state])
        if frame != self._shown:
            self._shown = frame
            lines = render(*frame, octants=self._octants).split("\n")
            for y, line in enumerate(lines):
                runs = list(re.finditer(r"[^ ]+", line.plain))
                for slot in range(RUNS_PER_ROW):
                    row = self._rows[y * RUNS_PER_ROW + slot]
                    row.display = slot < len(runs)
                    if row.display:
                        left, right = runs[slot].span()
                        row.styles.offset = (left, y)
                        row.styles.width = right - left
                        row.update(line[left:right])
