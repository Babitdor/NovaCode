"""Footer palette resolved from the ACTIVE Textual theme.

The status line, prompt row and info bar used to hardcode tokyo-night's neon
hexes (``#9ece6a``, ``#bb9af7``, ``#7aa2f7``, ...). Those are written into Rich
``Text`` styles, which CSS never touches, so the footer stayed neon after
``/theme`` switched to anything else — it did not follow the theme at all, and
under a muted palette like flexoki it read as bright-on-black.

This module derives the footer's colours from the theme's own attributes
(primary/accent/success/warning/error/foreground/background) so ``/theme``
recolours the footer like every other surface. Muted tones are blended toward
the background rather than hardcoded, so they work on light themes too.

Resolution is cached per ``(theme name, mode)`` — the footer repaints at 20 fps
while a turn streams, and the lookup walks the theme tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.color import Color, blend_rgb

if TYPE_CHECKING:
    from textual.theme import Theme


def _hex(color: str) -> str:
    """Normalize a theme colour attribute to ``#rrggbb``.

    Theme attributes are plain strings, but may be named colours, ``auto``, or
    carry alpha; anything unparseable falls back to a mid grey so a bad theme
    cannot crash a repaint.
    """
    if not color:
        return "#808080"
    try:
        tc = Color.parse(color).get_truecolor()
    except Exception:  # noqa: BLE001 — never let a colour break the status line
        return "#808080"
    else:
        return f"#{tc.red:02x}{tc.green:02x}{tc.blue:02x}"


def _mix(fg: str, bg: str, amount: float) -> str:
    """Blend *fg* toward *bg* by *amount* (0 = fg, 1 = bg)."""
    try:
        a = Color.parse(fg).get_truecolor()
        b = Color.parse(bg).get_truecolor()
        m = blend_rgb(a, b, max(0.0, min(1.0, amount)))
    except Exception:  # noqa: BLE001
        return fg
    else:
        return f"#{m.red:02x}{m.green:02x}{m.blue:02x}"


@dataclass(frozen=True)
class FooterPalette:
    """Resolved colours for the footer's Rich ``Text`` segments."""

    accent: str  # Nova activity + the branch value
    primary: str  # model name
    success: str  # "ready", a healthy context meter
    warning: str  # sandbox-off, ctx >= 75%
    error: str  # ctx >= 90%
    text: str  # primary values (workspace path)
    muted: str  # labels, counts
    faint: str  # separators, meter track
    dim: str  # bracket glyphs on the meter
    surface: str  # the bar's own background
    rail: str  # vertical separator glyph colour


def palette_for(theme: Theme) -> FooterPalette:
    """Build a :class:`FooterPalette` from a Textual ``Theme``.

    Args:
        theme: The active ``textual.theme.Theme``.

    Returns:
        Colours derived from that theme. A muted tone is the theme's foreground
        blended most of the way to its background, so it stays legible on both
        dark and light themes.
    """
    bg = _hex(getattr(theme, "background", "") or "#000000")
    fg = _hex(getattr(theme, "foreground", "") or "#ffffff")
    boost = getattr(theme, "boost", None)
    surface = _hex(boost) if boost else _mix(fg, bg, 0.93)

    return FooterPalette(
        accent=_hex(getattr(theme, "accent", "") or "#bb9af7"),
        primary=_hex(getattr(theme, "primary", "") or "#7aa2f7"),
        success=_hex(getattr(theme, "success", "") or "#9ece6a"),
        warning=_hex(getattr(theme, "warning", "") or "#e0af68"),
        error=_hex(getattr(theme, "error", "") or "#f7768e"),
        text=fg,
        muted=_mix(fg, bg, 0.42),
        faint=_mix(fg, bg, 0.72),
        dim=_mix(fg, bg, 0.58),
        surface=surface,
        rail=_mix(fg, bg, 0.78),
    )


# Cache keyed by (theme name, dark) so a /theme switch invalidates naturally.
_CACHE: dict[tuple[str, bool], FooterPalette] = {}


def cached_palette(theme: Theme) -> FooterPalette:
    """Resolve *theme* to a palette, memoized by theme name + mode."""
    key = (str(getattr(theme, "name", "?")), bool(getattr(theme, "dark", True)))
    hit = _CACHE.get(key)
    if hit is None:
        hit = palette_for(theme)
        _CACHE[key] = hit
    return hit
