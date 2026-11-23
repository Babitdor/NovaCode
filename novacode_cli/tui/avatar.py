"""Nova's transparent avatar, summoned with ``/nova``.

Graphics-capable terminals draw the source-resolution ``assets/nova-avatar-hq.png``
inline at the same compact size. Other terminals use ``assets/nova-avatar.png``
(built by ``scripts/build_avatar.py``; layout in ``avatar_frames.py``).
Each fallback terminal cell covers 2x4 of its pixels.
Where the terminal has octant glyphs (Unicode 16 "BLOCK OCTANT"), a cell shows all eight pixels as a
two-colour octant; elsewhere standard quadrant glyphs preserve left/right detail
with two vertical samples per cell. Animation follows what the agent is doing.
"""

from __future__ import annotations

import os
import re
import sys
import time
from functools import cache
from typing import TYPE_CHECKING, Any

from novacode_cli.ui.avatar import RGB

from rich.style import Style
from rich.text import Text
from textual.geometry import Region
from textual.widgets import Static

from novacode_cli.ui.avatar import (
    COUNTS,
    FPS,
    HEIGHT,
    NATIVE_SHEET,
    OCTANTS,
    REACTION_SECONDS,
    RUNS_PER_ROW,
    WIDTH,
    Cell,
    _dist,
    _representative,
    frame_pixels,
)
from novacode_cli.ui.avatar import cell as _pure_cell
from novacode_cli.ui.avatar import quadrant_cell as _pure_quadrant
from novacode_cli.ui.avatar_frames import NATIVE_CELL

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult

__all__ = [
    "COUNTS",
    "FPS",
    "HEIGHT",
    "OCTANTS",
    "REACTION_SECONDS",
    "RUNS_PER_ROW",
    "WIDTH",
    "NovaAvatar",
    "_dist",
    "_representative",
    "frame_pixels",
    "render",
]

#: How long the end-of-turn reaction plays before going back to idle.
_REACTION = REACTION_SECONDS


@cache
def native_image_type() -> Any:  # noqa: ANN401 - Optional image widget class.
    """Probe graphics before Textual owns stdin; never probe redirected/headless input."""
    if not NATIVE_CELL or not all(
        stream is not None and stream.isatty() for stream in (sys.__stdin__, sys.__stdout__)
    ):
        return None
    try:
        from textual_image.widget import Image, SixelImage, TGPImage

        if Image is SixelImage:
            from novacode_cli.tui.avatar_images import SixelAvatarImage

            return SixelAvatarImage
        return Image if Image is TGPImage else None
    except (ImportError, OSError, ValueError, TimeoutError):
        return None


@cache
def native_frame(state: str, index: int) -> Any:  # noqa: ANN401 - Lazily imported PIL image.
    """Keep source-resolution artwork for inline graphics, independent of cell rendering."""
    from PIL import Image

    position = list(COUNTS).index(state) * 2 + index
    x, y = position % 4 * NATIVE_CELL, position // 4 * NATIVE_CELL
    with Image.open(NATIVE_SHEET) as sheet:
        return sheet.crop((x, y, x + NATIVE_CELL, y + NATIVE_CELL)).convert("RGBA")


def _styled(value: Cell) -> tuple[str, Style | None]:
    """One shared cell as the Rich pair the widget draws."""
    glyph, foreground, background = value
    if not foreground and not background:
        return glyph, None
    return glyph, Style(color=foreground or None, bgcolor=background or None)


def _octant_cell(px: list[RGB | None]) -> tuple[str, Style | None]:
    """Eight pixels -> one glyph in at most two colours."""
    return _styled(_pure_cell(px))


def _quadrant_cell(px: list[RGB | None]) -> tuple[str, Style | None]:
    """Keep both pixel columns using the widely supported 2x2 quadrant glyphs."""
    return _styled(_pure_quadrant(px))


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
    cell = _octant_cell if octants else _quadrant_cell
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
    NovaAvatar > .avatar-image {
        overlay: screen; position: absolute; visibility: visible;
        padding: 0; border: none; background: transparent;
    }
    """

    def __init__(self, **kwargs: Any) -> None:  # noqa: D107
        super().__init__("", **kwargs)
        self.styles.width = WIDTH
        self.styles.height = HEIGHT
        self._mood = "idle"
        self._mood_until = 0.0
        self._click_until = 0.0
        self._idle_since = time.monotonic()
        self._octants = octants_supported()
        self._shown: tuple[str, int] | None = None
        self._rows = [Static("", classes="avatar-row") for _ in range(HEIGHT * RUNS_PER_ROW)]
        image_type = native_image_type()
        self._native = image_type(classes="avatar-image") if image_type else None
        if self._native is not None:
            self._native.styles.width = WIDTH
            self._native.styles.height = HEIGHT
            for row in self._rows:
                row.display = False

    def compose(self) -> ComposeResult:
        """Draw occupied glyph runs; the rectangular host never paints."""
        yield from self._rows
        if self._native is not None:
            yield self._native

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

    def on_click(self, event: events.Click) -> None:
        """A left click gets an immediate reaction, even during an active turn."""
        if event.button != 1:
            return
        event.stop()
        self.react("happy")
        self._click_until = self._mood_until
        self.animate_to(busy=False, talking=False)

    def animate_to(self, *, busy: bool, talking: bool, working: bool = False) -> None:
        """Advance the animation; repaints only when the frame changes."""
        now = time.monotonic()
        if now < self._click_until:
            state = "happy"
        elif busy:
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
            if self._native is not None:
                try:
                    self._native.image = native_frame(*frame)
                    return
                except (OSError, ValueError):
                    self._native.display = False
                    self._native = None
                    self.notify(
                        "Inline avatar unavailable; using character rendering", severity="warning"
                    )
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
