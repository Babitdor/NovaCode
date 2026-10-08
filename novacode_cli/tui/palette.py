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

Resolution is cached per theme colours and mode — the footer repaints at 20 fps
while a turn streams, and the lookup walks the theme tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.color import Color, blend_rgb
from rich.theme import Theme
from textual.color import Color as CSSColor

if TYPE_CHECKING:
    from rich.console import Console
    from textual.theme import Theme as TextualTheme


def _hex(color: str, fallback: str = "#808080", background: str | None = None) -> str:
    """Normalize a theme colour attribute to ``#rrggbb``.

    Theme attributes are plain strings, but may be named colours, ``auto``, or
    carry alpha; anything unparseable uses the supplied fallback so a bad theme
    cannot crash a repaint.
    """
    if not color or color in {"ansi_default", "auto"}:
        return fallback
    try:
        # Textual uses CSS and ansi_* names, which Rich's parser doesn't accept.
        parts = color.rsplit(maxsplit=1)
        value, opacity = parts if len(parts) == 2 and parts[1].endswith("%") else (color, "")
        parsed = CSSColor.parse(value)
        alpha = parsed.a
        if opacity:
            alpha *= float(opacity[:-1]) / 100
        tc = parsed.rich_color.get_truecolor()
        result = f"#{tc.red:02x}{tc.green:02x}{tc.blue:02x}"
        return _mix(result, background or fallback, 1 - alpha) if alpha < 1 else result
    except Exception:  # noqa: BLE001 — never let a colour break the status line
        return fallback


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
    #: Speaker-label colours for the transcript's role headers. Derived from the
    #: theme for the same reason the footer is: a hardcoded ANSI name here is not
    #: themeable, so ``/theme`` could never move it.
    user_label: str  # the "You" header
    agent_label: str  # the agent's own name header
    #: Tool-call rendering. The category heading used to be a literal ``#7aa2f7``
    #: and the status marks were emoji (⏳/✓/✗), neither of which ``/theme`` could
    #: touch — the heading stayed tokyo-night blue under every palette, and the
    #: emoji rendered in the terminal's own colour, not the theme's.
    tool_heading: str  # the "Explored — 3 reads" category heading
    tool_ok: str  # a succeeded call's bullet
    tool_fail: str  # a failed call's bullet
    tool_pending: str  # a still-running call's bullet


def palette_for(theme: Theme) -> FooterPalette:
    """Build a :class:`FooterPalette` from a Textual ``Theme``.

    Args:
        theme: The active ``textual.theme.Theme``.

    Returns:
        Colours derived from that theme. A muted tone is the theme's foreground
        blended most of the way to its background, so it stays legible on both
        dark and light themes.
    """
    dark = getattr(theme, "dark", True)
    bg = _hex(getattr(theme, "background", "") or "", "#000000" if dark else "#ffffff")
    fg = _hex(getattr(theme, "foreground", "") or "", "#ffffff" if dark else "#000000", bg)
    boost = getattr(theme, "boost", None)
    surface = _hex(boost, _mix(fg, bg, 0.93), bg) if boost else _mix(fg, bg, 0.93)

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
        # The two speakers are told apart by hue, not brightness: the user's own
        # turns take the theme's primary, the agent's take the accent. Those are
        # distinct attributes on both shipped themes (tokyo-night #7aa2f7 vs
        # #bb9af7, matrix #00ff41 vs #39ff14), so the labels stay separable in
        # every palette instead of collapsing into two shades of one hue.
        user_label=_hex(getattr(theme, "primary", "") or "#7aa2f7"),
        agent_label=_hex(getattr(theme, "accent", "") or "#bb9af7"),
        # Tool calls: the heading takes the theme's primary (the same attribute
        # the footer's model name uses, so the two read as one family), and the
        # status bullets take the theme's own success/error/warning so a failed
        # call is the same red as a failed context meter.
        tool_heading=_hex(getattr(theme, "primary", "") or "#7aa2f7"),
        tool_ok=_hex(getattr(theme, "success", "") or "#9ece6a"),
        tool_fail=_hex(getattr(theme, "error", "") or "#f7768e"),
        tool_pending=_hex(getattr(theme, "warning", "") or "#e0af68"),
    )


# ── Speaker labels, on the transcript's role headers ─────────────────────────

#: The main agent's assistant id. It has no per-agent colour of its own, so its
#: label follows the theme rather than an identity colour.
MAIN_AGENT_ID = "nova-agent"

#: Every spelling the main agent's header goes by. ``get_agent_display_name`` title
#: cases the id ("nova-agent" -> "Nova Agent") while the replay path and the
#: startup banner write the short form, so both have to be recognised or the main
#: agent silently keeps a pinned colour.
MAIN_AGENT_LABELS = frozenset({"nova", "nova agent"})


def user_label_style(palette: FooterPalette) -> str:
    """Rich style for the ``You`` role header, in *palette*'s colours."""
    return f"bold {palette.user_label}"


def agent_label_style(palette: FooterPalette, name: str, agent_color: str | None = None) -> str:
    """Rich style for the agent's own role header.

    Args:
        palette: Colours derived from the active theme.
        name: The header's own name, which is what decides whose colour this is.
        agent_color: The assistant's registered identity colour, when it has one.
            A named subagent (ralph) carries an explicit colour from its
            ``agent.md`` frontmatter and that colour is what distinguishes it from
            the main agent, so it is honoured as given.

    Returns:
        A Rich style string. Never an ANSI name, so the label is always theme- or
        identity-derived rather than a fixed terminal colour.

    Notes:
        The main agent is identified by *name* rather than by the absence of a
        colour, because the agent loop still hands down a legacy hardcoded green
        (``COLORS["success"]``) for it. Honouring that would reintroduce the exact
        bug this replaces: a pinned colour no ``/theme`` can move.
    """
    if not is_main_agent_label(name):
        candidate = (agent_color or "").strip()
        # An ANSI name ("green") is not themeable and was the original bug, so only
        # a real hex counts as an identity colour.
        if candidate.startswith("#"):
            return f"bold {candidate}"
    return f"bold {palette.agent_label}"


def is_main_agent_label(name: str) -> bool:
    """Whether *name* is a spelling of the main agent's role header."""
    return name.strip().lower() in MAIN_AGENT_LABELS


# Include colour values so replacing a theme under the same name refreshes it.
_CACHE: dict[tuple, FooterPalette] = {}


def cached_palette(theme: TextualTheme) -> FooterPalette:
    """Resolve *theme* to a palette, memoized by theme values."""
    key = tuple(
        getattr(theme, field, None)
        for field in (
            "name",
            "dark",
            "background",
            "foreground",
            "boost",
            "accent",
            "primary",
            "success",
            "warning",
            "error",
        )
    )
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

_MD_CACHE: dict[FooterPalette, Theme] = {}


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

    def luminance(color: str) -> float:
        channels = Color.parse(color).get_truecolor()
        linear = [v / 3294.6 if v <= 10 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    surface_luma = luminance(palette.surface)

    def contrast(color: str) -> float:
        luma = luminance(color)
        return (max(luma, surface_luma) + 0.05) / (min(luma, surface_luma) + 0.05)

    code = palette.primary
    if contrast(code) < 4.5:
        code = max((palette.text, "#ffffff", "#000000"), key=contrast)
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
        "markdown.code": f"bold {code} on {palette.surface}",
        "markdown.code_block": f"{code} on {palette.surface}",
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
    key = cached_palette(theme)
    hit = _MD_CACHE.get(key)
    if hit is None:
        hit = Theme(markdown_styles(key))
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
