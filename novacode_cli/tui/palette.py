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

Markdown was the same bug one layer down: Rich resolves its markdown elements
(``markdown.h2``, ``markdown.code``, ...) through the console's theme, and its
own defaults are ANSI names — ``underline magenta`` for h2, ``bold cyan on
black`` for inline code. ANSI is not themeable, so a reply's headings arrived in
whatever the terminal paints for magenta and stayed that way across ``/theme``.
:func:`markdown_styles` maps those elements onto the palette instead, and
:func:`apply_markdown_theme` installs them.

Resolution is cached per ``(theme name, mode)`` — the footer repaints at 20 fps
while a turn streams, and the lookup walks the theme tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.color import Color, blend_rgb
from rich.theme import Theme

if TYPE_CHECKING:
    from rich.console import Console
    from textual.theme import Theme as TextualTheme


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


def cached_palette(theme: TextualTheme) -> FooterPalette:
    """Resolve *theme* to a palette, memoized by theme name + mode."""
    key = (str(getattr(theme, "name", "?")), bool(getattr(theme, "dark", True)))
    hit = _CACHE.get(key)
    if hit is None:
        hit = palette_for(theme)
        _CACHE[key] = hit
    return hit


# ── Markdown, on Rich's side ────────────────────────────────────────────────

#: What each colour-carrying markdown element becomes. Rich's own *shapes* are
#: kept (h1 bold+underline, h2 underline, h3 bold, h4 italic), only the ANSI
#: colours are replaced. Elements Rich leaves colourless — paragraph, strong, em,
#: hr, h5/h6/h7 — are absent here and stay exactly as they were.
#: Consoles this module has pushed a markdown theme onto, so the next apply can
#: replace its own entry rather than stack another one.
_ARMED: list[Console] = []

_MD_CACHE: dict[str, Theme] = {}


def markdown_styles(palette: FooterPalette) -> dict[str, str]:
    """Rich style strings for markdown elements, in *palette*'s colours.

    Every value here replaced a hardcoded ANSI name: ``magenta`` for h2/h3/h4 and
    block quotes, ``cyan on black`` for code, ``cyan`` for list numbers and table
    borders, ``bright_blue``/``blue`` for links, ``bright_yellow`` for kbd.

    Args:
        palette: Colours derived from the active theme (see :func:`palette_for`).

    Returns:
        Style strings keyed by Rich's element names, ready for a
        ``rich.theme.Theme``.
    """
    return {
        # Headings. h1 had no colour of its own, so it gets the same one as its
        # siblings rather than inheriting the body colour and reading as plain text.
        "markdown.h1": f"bold underline {palette.primary}",
        "markdown.h2": f"underline {palette.primary}",
        "markdown.h3": f"bold {palette.primary}",
        "markdown.h4": f"italic {palette.primary}",
        "markdown.h5": f"bold {palette.muted}",
        "markdown.h6": palette.muted,
        "markdown.h7": f"dim {palette.muted}",
        # Code chips used to be a hardcoded black box on a themed reply.
        "markdown.code": f"bold {palette.primary} on {palette.surface}",
        "markdown.code_block": f"{palette.primary} on {palette.surface}",
        "markdown.block_quote": palette.muted,
        "markdown.item.bullet": f"bold {palette.accent}",
        "markdown.item.number": palette.accent,
        "markdown.list": palette.muted,
        "markdown.link": palette.primary,
        "markdown.link_url": f"underline {palette.muted}",
        "markdown.table.border": palette.muted,
        "markdown.table.header": f"bold {palette.primary}",
        "markdown.kbd": f"bold {palette.warning}",
    }


def markdown_theme(theme: TextualTheme) -> Theme:
    """A Rich ``Theme`` carrying the markdown colours for *theme*, memoized."""
    key = str(getattr(theme, "name", "?"))
    hit = _MD_CACHE.get(key)
    if hit is None:
        hit = Theme(markdown_styles(cached_palette(theme)))
        _MD_CACHE[key] = hit
    return hit


def apply_markdown_theme(console: Console, theme: TextualTheme) -> None:
    """Install *theme*'s markdown colours on *console*.

    The console's theme stack is how Rich resolves ``markdown.h2`` and friends, so
    this is the only lever: pass the renderable itself a style and the element
    lookup overrides it. Call it at startup and from the app's theme watcher, or
    ``/theme`` recolours every surface except the replies.

    A console Nova has already armed gets its own entry popped first, so a long
    session does not stack one theme per switch. Nothing else pushes a console
    theme here — neither Textual nor the rest of Nova does — so the top of that
    stack is ours.
    """
    # Built outside the guard below on purpose: a mistake in our own theme must
    # fail loudly in tests rather than be swallowed as an unmatched colour.
    resolved = markdown_theme(theme)
    try:
        if any(console is armed for armed in _ARMED):
            console.pop_theme()
        else:
            _ARMED.append(console)
        console.push_theme(resolved)
    except Exception:  # noqa: BLE001 — colours must never break a repaint
        return
