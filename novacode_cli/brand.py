"""Nova's startup banner: one lockup, one accent, one art source.

Every startup surface reads its art from here -- ``config.get_responsive_ascii``
(the TUI home banner and the pre-TUI boot screen), ``format_version_banner``,
and the compact marks used by the command help and the Cowork UI.

Two defects prompted this module:

* The art was copy-pasted across seven call sites, and its rows ranged from
  **26 to 79 cells** within one logo. That is why it read as a chunk torn out
  of its right side, and why at 80 columns it overflowed the 76 usable cells of
  ``#transcript { padding: 1 2 }`` and wrapped, jittering the logo.
* The surfaces disagreed about colour: the boot screen drew red, the TUI opened
  tokyo-night blue, and the rain logo fell back to a hardcoded green. Any of the
  three could drift without anyone noticing.

Layout: the art is **one lockup**, centred as a unit --::

    <portrait>   <wordmark>
                 v<version>

with the portrait kept, and the version alone under the wordmark rather than a
caption row running the width of the lockup. The wordmark already spells ``NOVA``
in box glyphs, so a trailing ``NOVA · a terminal coding agent · v<version>`` line
repeats the lockup's own ink twice over and is gone; only the version remains,
because it is the one fact the art cannot draw.

Deliberately **Textual-free**: ``config/config.py`` renders the pre-TUI boot
screen, and importing Textual there would drag it into the startup import graph
(Nova guards that graph closely -- see ``novacode_cli/_lazy_heavy.py``). Rich is
imported only for :func:`rich.cells.cell_len`, which is a leaf module.
"""

from __future__ import annotations

from functools import lru_cache

from rich.cells import cell_len

__all__ = [
    "FALLBACK_ACCENT",
    "LOCKUP_WIDTH",
    "PORTRAIT_WIDTH",
    "TAGLINE",
    "THEME_ACCENTS",
    "WIDE_MIN_WIDTH",
    "WORDMARK_LINES",
    "WORDMARK_WIDTH",
    "art_for",
    "compact_art",
    "compact_mark",
    "get_accent_hex",
    "lockup",
    "responsive_art",
    "version_line",
    "wordmark",
]

# ── the wordmark ────────────────────────────────────────────────────────────

#: Each row is exactly 42 cells wide. Generated from per-letter
#: blocks joined with a 1-cell gap, so every row is uniform and the right edge is
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

#: The narrowest terminal that shows the full lockup: the art plus the
#: transcript's horizontal inset (2 cells each side).
WIDE_MIN_WIDTH = 78

#: The caption's descriptive half. The lockup also carries the original art's
#: prose tagline; this is the plain-language line used by the one-line marks.
TAGLINE = "a terminal coding agent"


def wordmark() -> str:
    """The block NOVA wordmark as one string (no trailing newline)."""
    return "\n".join(WORDMARK_LINES)


def compact_mark() -> str:
    """The single-line mark used on terminals too narrow for any of the art."""
    return "◆ NOVA"


def version_line(version: str | None = None) -> str:
    """The caption under the wordmark: ``v<version>``, or empty without one.

    Just the version. The lockup's old caption was
    ``NOVA · a terminal coding agent · v<version>``, two thirds of which repeated
    what the art above it already draws -- the wordmark spells NOVA in 42 cells.
    """
    return f"v{version}" if version else ""


def _lockup_rows(version: str | None) -> list[str]:
    """The lockup rows, with *version*'s caption in the right-hand column."""
    column = [_pad(row, _COLUMN_WIDTH) for row in WORDMARK_LINES]
    blank, caption = " " * _COLUMN_WIDTH, _pad(version_line(version), _COLUMN_WIDTH)
    # The caption sits directly under the wordmark when there is one, and takes
    # the blank spacer row's place when there is not, so a versionless lockup is
    # exactly as tall as a versioned one -- the rain grid must not resize when a
    # version appears.
    column.append(blank)
    column.append(caption)

    height = len(_PORTRAIT)
    if len(column) > height:
        msg = f"caption column is {len(column)} rows, portrait is {height}"
        raise ValueError(msg)
    # The wordmark is pinned to the portrait's **top** edge rather than centred
    # against it: the two blocks read as one lockup only when their first rows
    # align, and centring a 6-row wordmark against a 12-row portrait would float
    # NOVA into the middle of the face. The rest of the column is blank.
    column.extend(" " * _COLUMN_WIDTH for _ in range(height - len(column)))

    rows: list[str] = []
    for i, portrait_row in enumerate(_PORTRAIT):
        row = _pad(portrait_row, PORTRAIT_WIDTH) + " " * _GAP + column[i]
        if cell_len(row) != LOCKUP_WIDTH:  # pragma: no cover - defensive
            msg = f"row {i} is {cell_len(row)} cells, expected {LOCKUP_WIDTH}"
            raise ValueError(msg)
        rows.append(row)
    return rows


# ── the portrait ────────────────────────────────────────────────────────────

#: Widest portrait row; every portrait row is padded to it.
PORTRAIT_WIDTH = 29

#: Columns between the portrait and the right-hand text column.
_GAP = 3

#: The transcript insets its content by 2 cells each side
#: (``#transcript { padding: 1 2 }``), so art sized to the terminal width
#: overflows by exactly this much and wraps.
_TRANSCRIPT_INSET = 4

#: The portrait, right-padded to :data:`PORTRAIT_WIDTH` with real spaces.
#: Interior blanks are braille blanks (U+2800), which the rain compositor paints;
#: only the padding is a real space, and only real spaces are transparent.
_PORTRAIT: tuple[str, ...] = (
    "⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⠿⠻⠟⠿⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿",
    "⣿⣿⣿⣿⣿⣿⡟⠋⢔⠈⠀⠀⠀⠀⠀⠀⠐⠉⠻⣿⣿⣿⣿⣿⣿",
    "⣿⣿⣿⣿⣿⠏⠀⠀⠀⠢⢄⡀⠀⠀⠀⠀⠀⠀⠀⣉⢿⣿⣿⣿⣿",
    "⣿⣿⣿⣿⠃⠀⠘⠄⠀⠀⠀⠑⠡⡆⠀⡀⣦⡐⠞⠈⢌⢻⣿⣿⣿",
    "⣿⣿⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⡠⢀⣴⣿⡷⢄⠀⠀⡌⢿⣿⣿",
    "⣿⣿⣿⠂⠀⠀⠀⠀⠀⠀⠀⠀⣀⣥⣬⣿⣿⡕⠉⠂⠀⠠⢸⣿⣿",
    "⣿⣿⡟⠀⠀⠀⠀⠀⠀⠀⣴⡋⣥⠀⢠⣿⣿⣅⣀⡃⠀⠀⠍⢻⣿",
    "⣿⣿⠀⠀⠀⠀⠀⢷⡰⢄⢻⣿⣞⣾⣿⣿⣿⣸⣿⠆⠀⠀⠈⡌⣿",
    "⡿⠁⠀⠀⠀⠀⠀⠀⠉⠁⡱⡻⡟⢿⣿⣭⣽⣾⠟⠀⠀⠀⠀⠘⡘",
    "⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⣽⣾⣳⣯⡙⠛⠁⠀⠀⠀⠆⠀⠀⠁",
    "⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⢿⣿⣿⡆⠀⠀⠀⠀⠀⠀⢀⣾",
    "⣿⣷⣤⠀⠀⠀⠀⠀⠀⠀⠀⣠⠻⡄⣿⣿⡇⠄⠀⠀⠀⠀⢐⢻⣿",
    "⣿⣿⣇⢿⣿⣶⣄⠀⠀⠀⠀⠘⠿⢸⣿⣿⡇⠀⠀⠀⠀⢀⣿⢗⣽",
    "⣿⣿⣿⣧⣝⠿⣿⣦⠀⠀⠀⠀⢀⣾⣿⣿⠁⠀⠀⠀⠀⢋⣵⣿⣿",
    "⣿⣿⣿⣿⣿⣿⣶⣮⣥⣀⣀⣀⣼⣿⣿⣿⣀⣀⣤⣶⣿⣿⣿⣿⣿",
)

#: The lockup's right-hand column carries the wordmark and nothing else. The
#: original art's four-line prose tagline used to sit under it, but it described
#: the same tool a second time behind the wordmark's own name. Empty on purpose:
#: the tuple is the seam a future caption would use, and ``lockup()`` still
#: honours it.
_LOCKUP_TAGLINE: tuple[str, ...] = ()

#: The version ``lockup()`` renders. The shipped startup art is versionless --
#: only ``art_for(width, version)`` supplies one -- so this stays ``None`` and
#: exists so the layout tests and ad-hoc renders can exercise the caption slot.
_LOCKUP_VERSION: str | None = None

#: The one-line name, for terminals too narrow even for the portrait.
_NAME_LINE = "♥ NOVA ~"

#: The right-hand column must hold the version caption as well as the wordmark.
#: ``v<version>`` is well under the wordmark's 42 cells, but the width is
#: computed rather than assumed so an over-long version cannot overflow the field
#: and raise.
_CAPTION_WIDTH = max(cell_len(line) for line in (" ".join(_LOCKUP_TAGLINE), version_line("0.0.0")))
_COLUMN_WIDTH = max(WORDMARK_WIDTH, _CAPTION_WIDTH)

#: The lockup's total width -- uniform across every row, by construction.
LOCKUP_WIDTH = PORTRAIT_WIDTH + _GAP + _COLUMN_WIDTH


def _pad(text: str, width: int) -> str:
    """Right-pad *text* to *width* **cells** with real spaces.

    Uses :func:`rich.cells.cell_len` rather than ``len``: a caption may carry
    U+2665, and any character where the two disagree would be padded to a width
    that is uniform in Python and ragged on screen. (U+2665 followed by its
    U+FE0E variation selector is exactly such a pair -- two codepoints, one
    cell -- so the art carries the bare U+2665.)

    The padding character is a real space (U+0020) deliberately. The rain
    compositor's transparency test is exactly ``ch == " "``, so padding with a
    braille blank (U+2800, which is not whitespace and which ``rstrip`` leaves
    alone) would paint an opaque border around the art and occlude the rain.
    """
    deficit = width - cell_len(text)
    if deficit < 0:
        msg = f"overflow: {cell_len(text)} cells into a {width}-cell field: {text!r}"
        raise ValueError(msg)
    return text + " " * deficit


@lru_cache(maxsize=1)
def lockup() -> tuple[str, ...]:
    """The full art: portrait and wordmark side by side, caption beneath.

    Returns:
        One string per row, every row exactly :data:`LOCKUP_WIDTH` cells wide.

    Raises:
        ValueError: If a source block is wider than its field, meaning the art
            and this module's geometry have diverged.
    """
    return tuple(_lockup_rows(_LOCKUP_VERSION))


@lru_cache(maxsize=1)
def compact_art() -> tuple[str, ...]:
    """The portrait with the name beneath it, for terminals too narrow to lockup.

    Drops the wordmark and the version caption, which cannot fit beside the
    portrait, but keeps the portrait itself -- the art is the point of the
    banner, so the narrow variant should not throw it away.

    Returns:
        One string per row, every row exactly :data:`PORTRAIT_WIDTH` cells wide.
    """
    rows = [_pad(row, PORTRAIT_WIDTH) for row in _PORTRAIT]
    rows.append(" " * PORTRAIT_WIDTH)
    rows.append(_pad(_NAME_LINE.center(PORTRAIT_WIDTH), PORTRAIT_WIDTH))
    return tuple(rows)


def art_for(width: int | None = None, version: str | None = None) -> str:
    """Responsive startup art for *width* terminal columns.

    Args:
        width: Terminal width in cells. ``None`` is treated as 80.
        version: Optional version, printed as a caption just under the wordmark.
            This is the only caption the lockup gets: the appended
            ``NOVA · a terminal coding agent · v<version>`` row was dropped as a
            repeat of the wordmark the art draws directly above it.

    Returns:
        The lockup on wide terminals, the portrait alone when the lockup cannot
        fit beside it, or the one-line mark when even the portrait cannot. Never
        ragged, never empty, and never wider than the columns the banner gets.

    The banner is centred by its consumer, so the art is returned flush-left and
    every row within a tier is the same width.
    """
    columns = width or 80
    usable = columns - _TRANSCRIPT_INSET
    if usable >= LOCKUP_WIDTH:
        return "\n" + "\n".join(_lockup_rows(version)) + "\n"
    if usable >= PORTRAIT_WIDTH:
        return "\n" + "\n".join(compact_art()) + "\n"
    return f"\n{compact_mark()}\n"


def responsive_art(width: int | None = None) -> str:
    """Alias for :func:`art_for` without the version caption.

    Kept so callers that only want the art do not have to pass ``version=None``
    explicitly, and so the name reads the same as it did before the merge.
    """
    return art_for(width)


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
