"""Nova's startup branding: one wordmark, one accent.

Three surfaces used to disagree about both:

* the boot animation and the ``--version`` banner drew a **red** (#ef4444)
  braille-blob-plus-ANSI logo,
* the TUI opened on tokyo-night **blue** (#7aa2f7),
* the Matrix-rain logo fell back to **green** (#00ff88).

So the splash never matched the app it launched, and the art itself was
copy-pasted in four places (``config.py`` x4) with ragged right edges -- rows
of 29, 44, 50 and 77 columns in the same logo, which read as a chunk torn out
of its right side.

This module owns the art and the accent; every surface imports from here. Any
future logo change is a one-line edit in one file.

Deliberately **Textual-free**: ``config/config.py`` renders the pre-TUI boot
screen, and importing Textual there would pull it into the startup import
graph (Nova guards that graph closely -- see ``novacode_cli/_lazy_heavy.py``).
"""

from __future__ import annotations

__all__ = [
    "TAGLINE",
    "WIDE_MIN_WIDTH",
    "WORDMARK_LINES",
    "art_for",
    "compact_mark",
    "get_accent_hex",
    "tagline_line",
    "wordmark",
]

#: Each row is exactly 43 cells wide. Generated from per-letter 10-cell blocks
#: joined with a 1-cell gap, so every row is uniform and the right edge is
#: straight -- the property the old art lacked.
WORDMARK_LINES: tuple[str, ...] = (
    "███╗   ██╗  ██████╗   ██╗   ██╗   █████╗  ",
    "████╗  ██║ ██╔═══██╗  ██║   ██║  ██╔══██╗ ",
    "██╔██╗ ██║ ██║   ██║  ██║   ██║  ███████║ ",
    "██║╚██╗██║ ██║   ██║  ╚██╗ ██╔╝  ██╔══██║ ",
    "██║ ╚████║ ╚██████╔╝   ╚████╔╝   ██║  ██║ ",
    "╚═╝  ╚═══╝  ╚═════╝     ╚══╝     ╚═╝  ╚═╝ ",
)

#: The wordmark's own width, in cells. Uniform by construction.
WORDMARK_WIDTH = max(len(line) for line in WORDMARK_LINES)

#: Below this terminal width the block wordmark is replaced by the compact mark.
WIDE_MIN_WIDTH = 60

TAGLINE = "a terminal coding agent"


def wordmark() -> str:
    """The block NOVA wordmark as one string (no trailing newline)."""
    return "\n".join(WORDMARK_LINES)


def compact_mark() -> str:
    """The single-line mark used on terminals too narrow for the block art."""
    return "◆ NOVA"


def tagline_line(version: str | None = None) -> str:
    """The caption under the wordmark: name, tagline, and version.

    Carries a literal ``NOVA`` on purpose -- the block art spells NOVA out of
    box-drawing glyphs, so anything searching the *text* of the banner (the TUI
    test does) needs one real occurrence of the name.
    """
    parts = ["NOVA", TAGLINE]
    if version:
        parts.append(f"v{version}")
    return " · ".join(parts)


def art_for(width: int | None, version: str | None = None) -> str:
    """Responsive startup art for *width* terminal columns.

    Args:
        width: Terminal width in cells. ``None`` is treated as 80.
        version: Optional version string appended to the caption.

    Returns:
        The block wordmark plus its caption on wide terminals, or the compact
        single-line mark on narrow ones. Never ragged, never empty.
    """
    columns = width or 80
    if columns < WIDE_MIN_WIDTH:
        return f"\n{compact_mark()}\n"
    caption = tagline_line(version)
    return "\n" + wordmark() + "\n" + caption.rjust((WORDMARK_WIDTH + len(caption)) // 2) + "\n"


# The theme accents, mirrored from the Textual themes in ``tui/widgets.py``.
# Imported by name rather than by import because that module needs Textual;
# ``test_brand.py`` asserts this table stays in step with the real themes.
THEME_ACCENTS: dict[str, str] = {
    "tokyo-night": "#7aa2f7",
    "matrix": "#00ff41",
}
DEFAULT_THEME = "tokyo-night"
FALLBACK_ACCENT = "#7aa2f7"


def get_accent_hex() -> str:
    """The accent colour of the theme Nova will actually open with.

    Reads the persisted ``theme`` setting so the pre-TUI boot screen matches
    the TUI that follows it. Resolved lazily and defensively: a missing config,
    an unreadable file, or an unknown theme name must never break startup --
    all of those fall back to the default theme's accent.
    """
    try:
        from novacode_cli.config.nova_config import NovaConfig

        name = NovaConfig().get("theme") or DEFAULT_THEME
    except Exception:  # noqa: BLE001 -- branding must never break startup
        return FALLBACK_ACCENT
    return THEME_ACCENTS.get(str(name), FALLBACK_ACCENT)
