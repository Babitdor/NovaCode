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


def scene_frame_layout(
    style: str, plain: str, accent: str, text_color: str, frame: int
) -> tuple[str, list[tuple[int, int, str]]]:
    """One scene frame as plain data: the text plus `(start, end, style)` spans.

    Pure and string-only so the per-frame compute can be ported to Rust and
    verified against recorded calls; :func:`scene_frame` is the Rich wrapper.
    """
    if style == "terminal":
        visible = min(len(plain), 2 + frame * 3)
        chars = list(plain)
        for index in range(visible, len(chars)):
            if chars[index] != "\n":
                chars[index] = " "
        text = "".join(chars)
        cursor_on = frame % (_BLINK_HALF_FRAMES * 2) < _BLINK_HALF_FRAMES
        if visible < len(text):
            cursor = visible + (text[visible] == "\n")
            if cursor < len(text) and cursor_on:
                text = text[:cursor] + "_" + text[cursor + 1 :]
        else:
            text += " _" if cursor_on else "  "
        return text, []
    if style == "blueprint":
        start = plain.find("─")
        if start >= 0:
            end = plain.find("\n", start)
            end = len(plain) if end < 0 else end
            cursor = start + frame % max(1, end - start)
            return plain, [(cursor, cursor + 1, f"bold reverse {accent}")]
        return plain, []
    if style == "studio":
        lines = plain.splitlines(keepends=True)
        sweep = frame % max(1, len(lines))
        spans: list[tuple[int, int, str]] = []
        offset = 0
        stars = list(plain)
        for index, line in enumerate(lines):
            if index == sweep:
                spans.append((offset, offset + len(line.rstrip("\n")), f"bold {text_color}"))
            offset += len(line)
        # Only ordinary spaces are transparent: braille blanks belong to the portrait.
        for index in range(frame % 7, len(stars), 97):
            if stars[index] == " ":
                stars[index] = "." if (index + frame) % 2 else "*"
        return "".join(stars), spans
    return plain, []


def scene_frame(style: str, base: Text, palette: FooterPalette, frame: int) -> Text:
    """Draw one decorative frame without changing the scene's row count."""
    text, spans = scene_frame_layout(style, base.plain, palette.accent, palette.text, frame)
    if style == "terminal":
        return Text(text, style=base.style)
    # The base art's own spans ride along (the original did this via
    # base.copy()); the star mutation in studio keeps offsets valid.
    result = Text(text, style=base.style, spans=list(base.spans))
    for start, end, span_style in spans:
        result.stylize(span_style, start, end)
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
        from novacode_cli.tui.app import _paint

        frame = max(0, int((now - self._started) * HOME_SCENE_FPS)) if animated else -1
        state = (animated, frame)
        if self._palette is None or state == self._shown:
            return
        self._shown = state
        # Scene frames keep the base art's rows and width, so they only need a
        # repaint: _paint skips the full-screen layout Static.update triggers.
        _paint(
            self,
            scene_frame(self._scene, self._base, self._palette, frame)
            if animated
            else self._base.copy(),
        )
