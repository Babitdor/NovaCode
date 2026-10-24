"""Small themed home scenes driven by Nova's existing animation clock."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from novacode_cli.tui.palette import FooterPalette

ANIMATED_HOME_STYLES = frozenset({"terminal", "studio", "blueprint"})
HOME_SCENE_FPS = 6
HOME_MOTION_MIN_WIDTH = 60
HOME_MOTION_MIN_HEIGHT = 24
PORTRAIT_HOME_MIN_WIDTH = 90
PORTRAIT_HOME_MIN_HEIGHT = 34
_BLINK_HALF_FRAMES = 3


def scene_frame(style: str, base: Text, palette: FooterPalette, frame: int) -> Text:
    """Draw one decorative frame without changing the scene's row count."""
    result = base.copy()
    if style == "terminal":
        visible = min(len(base.plain), 2 + frame * 3)
        text = "".join(
            char if index < visible or char == "\n" else " "
            for index, char in enumerate(base.plain)
        )
        cursor_on = frame % (_BLINK_HALF_FRAMES * 2) < _BLINK_HALF_FRAMES
        if visible < len(text):
            cursor = visible + (text[visible] == "\n")
            if cursor < len(text) and cursor_on:
                text = text[:cursor] + "_" + text[cursor + 1 :]
        else:
            text += " _" if cursor_on else "  "
        return Text(text, style=base.style)
    if style == "blueprint":
        start = base.plain.find("─")
        if start >= 0:
            end = base.plain.find("\n", start)
            end = len(base.plain) if end < 0 else end
            cursor = start + frame % max(1, end - start)
            result.stylize(f"bold reverse {palette.accent}", cursor, cursor + 1)
    elif style == "studio":
        lines = base.plain.splitlines(keepends=True)
        sweep = frame % max(1, len(lines))
        offset = 0
        for index, line in enumerate(lines):
            if index == sweep:
                result.stylize(f"bold {palette.text}", offset, offset + len(line.rstrip("\n")))
            offset += len(line)
        # Only ordinary spaces are transparent: braille blanks belong to the portrait.
        stars = list(base.plain)
        for index in range(frame % 7, len(stars), 97):
            if stars[index] == " ":
                stars[index] = "." if (index + frame) % 2 else "*"
        result = Text("".join(stars), style=result.style, spans=result.spans)
    return result


class HomeScene(Static):
    """Render scene frames without creating a separate timer or worker."""

    def __init__(self, *, id: str) -> None:  # noqa: A002 -- Textual widget API.
        """Start empty; the app supplies the current art and palette."""
        super().__init__("", id=id)
        self._scene = ""
        self._base = Text()
        self._palette: FooterPalette | None = None
        self._started = 0.0
        self._shown: tuple[bool, int] | None = None

    def configure(self, style: str, base: Text, palette: FooterPalette, now: float) -> None:
        """Update colors and content without restarting a running opening effect."""
        if style != self._scene:
            self._started = now
        if style != self._scene or base != self._base or palette != self._palette:
            self._shown = None
        self._scene, self._base, self._palette = style, base.copy(), palette

    def advance(self, now: float, *, animated: bool) -> None:
        """Paint only a changed frame; disabled motion displays complete static art."""
        frame = max(0, int((now - self._started) * HOME_SCENE_FPS)) if animated else -1
        state = (animated, frame)
        if self._palette is None or state == self._shown:
            return
        self._shown = state
        self.update(
            scene_frame(self._scene, self._base, self._palette, frame)
            if animated
            else self._base.copy()
        )
