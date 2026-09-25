"""Nova's startup banner: one lockup, one file, every surface.

The logo used to be copy-pasted across seven call sites -- four tiers inside
``config.get_responsive_ascii``, plus ``format_version_banner``,
``BootAnimation`` and the Cowork web UI -- and its rows ranged from **26 to 79
cells** in the same logo. That is why the banner read as a chunk torn out of
its right side, and why at 80 columns it overflowed the 76 usable cells of
``#transcript { padding: 1 2 }`` and wrapped, jittering the logo.

This module owns the banner art for the surfaces that have been migrated to it:
``config.get_responsive_ascii`` and ``config.format_version_banner`` are now
thin adapters. The pre-TUI ``BootAnimation`` and the Cowork web UI still carry
their own copies of the table, unchanged by this work.

Every row is padded to the same width by construction, and
:func:`responsive_art` is guaranteed not to return anything wider than the space
the banner actually gets.

Layout (keep the portrait, recompose the lockup)::

    <portrait>   <wordmark>
                 <tagline>

composed as one block and centred as a unit, instead of the tagline being
indented to a ragged gutter part-way into the art.

``config.get_responsive_ascii`` keeps its name and signature: ``tui/app.py`` and
the TUI tests call it, so it stays the public way in, and this module stays the
place the art actually lives.

Deliberately **Textual-free**: ``config/config.py`` renders the pre-TUI boot
screen, and importing Textual there would pull it into the startup import graph
(Nova guards that graph closely -- see ``novacode_cli/_lazy_heavy.py``). Rich is
already imported by that module for the same reason it is used here.
"""

from __future__ import annotations

from functools import lru_cache

from rich.cells import cell_len

__all__ = [
    "LOCKUP_WIDTH",
    "PORTRAIT_WIDTH",
    "compact_art",
    "lockup",
    "responsive_art",
]

#: Widest portrait row; every portrait row is padded to it.
PORTRAIT_WIDTH = 29

#: Columns between the portrait and the right-hand text column.
_GAP = 3

#: Widest of the wordmark (39 cells) and the longest tagline line (40 cells).
_COLUMN_WIDTH = 40

#: The lockup's total width -- uniform across every row, by construction.
LOCKUP_WIDTH = PORTRAIT_WIDTH + _GAP + _COLUMN_WIDTH

#: The transcript insets its content by 2 cells each side
#: (``#transcript { padding: 1 2 }`` in ``tui/app.py``), so art sized to the
#: full terminal width overflows by exactly this much.
_TRANSCRIPT_INSET = 4

#: The portrait, verbatim from the shipped art, right-padded to
#: :data:`PORTRAIT_WIDTH` with real spaces. Interior blanks are braille blanks
#: (U+2800), which the compositor paints; only the padding is transparent.
_PORTRAIT: tuple[str, ...] = (
    "⣿⣿⣿⣿⣟⠊⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿  ",
    "⣿⣿⣿⡏⠁⠀⠀⠀⠀⠀⠀⢀⣰⣶⣶⡄⠀⠀⠀⠀⠀⠀⢀⠀⠀⠈⢻  ",
    "⣿⣿⣿⠁⠄⠀⠀⠀⠀⠀⣤⣾⣿⣿⣿⣿⡂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠽ ",
    "⣿⣿⡏⣸⠀⠀⠀⠀⢀⣼⣿⣿⣿⣿⣿⣿⣿⡆⠀⠀⠈⠀⠀⠀⠀⠀⠀⠰ ",
    "⣿⣿⡇⠁⠀⠀⠀⣤⣍⣙⣿⣿⣏⣠⠄⠲⠲⠦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻",
    "⣿⣿⠁⠀⠀⠀⠀⠀⢤⠙⣿⣿⣿⣇⣀⡐⢂⣠⡄⠠⠀⠀⠀⠀⠀⠀⡀⢠⢸",
    "⣿⣿⠀⠀⠐⠀⣶⣷⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠐⠈⠀⠀⠀⠉⠘⣼",
    "⣿⣿⠀⠈⠀⠀⣿⣿⣿⡿⣿⠿⢿⣿⣿⣿⣿⣿⣿⣧⡀⠀⢄⠲⠀⠀⠀⣱ ",
    "⣿⣿⡆⠀⠀⠀⠈⣿⣿⣷⣶⣼⣾⣿⣿⣿⣿⣿⣿⣿⣷⠂⠀⠀⠂⢀⢲  ",
    "⣿⣿⣿⡆⠀⠀⠀⠙⣿⠋⠠⠄⢀⠉⣹⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⣿  ",
    "⣿⣿⣿⣿⣦⠀⠀⠀⠘⣿⣤⣤⣶⣿⣿⣿⣿⣿⠟⣛⡽⠀⠀⠀⠠⣸   ",
    "⣿⣿⣿⣿⣿⣷⡀⠀⠀⠈⠻⣿⣿⣿⠿⠛⠋⠐⠚⠛⠃   ⣰⣿   ",
)

#: The block NOVA wordmark, verbatim, padded to :data:`_COLUMN_WIDTH`.
_WORDMARK: tuple[str, ...] = (
    "███╗   ██╗  ██████╗  ██╗   ██╗  █████╗  ",
    "████╗  ██║ ██╔═══██╗ ██║   ██║ ██╔══██╗ ",
    "██╔██╗ ██║ ██║   ██║ ██║   ██║ ███████║ ",
    "██║╚██╗██║ ██║   ██║ ╚██╗ ██╔╝ ██╔══██║ ",
    "██║ ╚████║ ╚██████╔╝  ╚████╔╝  ██║  ██║ ",
    "╚═╝  ╚═══╝  ╚═════╝    ╚═══╝   ╚═╝  ╚═╝ ",
)

#: The caption, verbatim. Line 1 is the longest at 40 cells and sets
#: :data:`_COLUMN_WIDTH`; 3 is short and gets centred in the column.
_TAGLINE: tuple[str, ...] = (
    "~ Secrets, Locks, Firewalls",
    "Everything has a weakness.",
    "The right code just knows where to look.",
    "♥ NOVA ~",
)

#: The one-line name, for terminals too narrow even for the portrait. Bare
#: U+2665, no U+FE0E variation selector -- see the note on _TAGLINE above.
_NAME_LINE = "\u2665 NOVA ~"


def _pad(text: str, width: int) -> str:
    """Right-pad *text* to *width* **cells** with real spaces.

    Uses :func:`rich.cells.cell_len` rather than ``len``: the tagline contains
    U+2665 U+FE0E, where the codepoint count and the rendered width disagree, so
    ``len``-based padding would produce rows that are uniform in Python and
    ragged on screen.

    The padding character is a real space (U+0020) on purpose. The rain
    compositor's transparency test is exactly ``ch == " "``, so padding with a
    braille blank (U+2800, not whitespace, so ``rstrip`` would not even remove
    it) would paint an opaque border around the art and occlude the rain.
    """
    deficit = width - cell_len(text)
    if deficit < 0:
        msg = f"overflow: {cell_len(text)} cells into a {width}-cell field: {text!r}"
        raise ValueError(msg)
    return text + " " * deficit


@lru_cache(maxsize=1)
def lockup() -> tuple[str, ...]:
    """The full lockup: portrait and wordmark side by side, tagline beneath.

    Returns:
        One string per row, every row exactly :data:`LOCKUP_WIDTH` cells wide.

    Raises:
        ValueError: If a source block is wider than its field, which would mean
            the art and this module's geometry have diverged.
    """
    # Right-hand column: wordmark, a blank separator row, then the caption
    # centred within the column so its short lines sit under the wordmark.
    column = [_pad(row, _COLUMN_WIDTH) for row in _WORDMARK]
    column.append(" " * _COLUMN_WIDTH)
    column.extend(_pad(line.center(_COLUMN_WIDTH), _COLUMN_WIDTH) for line in _TAGLINE)

    height = len(_PORTRAIT)
    if len(column) > height:
        msg = f"caption column is {len(column)} rows, portrait is {height}"
        raise ValueError(msg)
    # Centre the column against the portrait so the lockup's optical centre
    # matches its geometric centre.
    top = (height - len(column)) // 2

    rows: list[str] = []
    for i, portrait_row in enumerate(_PORTRAIT):
        j = i - top
        right = column[j] if 0 <= j < len(column) else " " * _COLUMN_WIDTH
        row = _pad(portrait_row, PORTRAIT_WIDTH) + " " * _GAP + right
        if cell_len(row) != LOCKUP_WIDTH:  # pragma: no cover - defensive
            msg = f"row {i} is {cell_len(row)} cells, expected {LOCKUP_WIDTH}"
            raise ValueError(msg)
        rows.append(row)
    return tuple(rows)


@lru_cache(maxsize=1)
def compact_art() -> tuple[str, ...]:
    """The portrait with the name beneath it, for narrow terminals.

    Drops the wordmark and the long caption (which cannot fit alongside the
    portrait) but keeps the portrait itself -- the art is the point of the
    banner, so the narrow variant should not throw it away.

    Returns:
        One string per row, every row at most :data:`PORTRAIT_WIDTH` cells wide.
    """
    rows = [_pad(row, PORTRAIT_WIDTH) for row in _PORTRAIT]
    rows.append(" " * PORTRAIT_WIDTH)
    rows.append(_pad(_NAME_LINE.center(PORTRAIT_WIDTH), PORTRAIT_WIDTH))
    return tuple(rows)


def responsive_art(width: int | None = None) -> str:
    """The startup banner art for a *width*-column terminal.

    Args:
        width: Terminal width in cells. ``None`` is treated as 80.

    Returns:
        The art as one string, never wider than the columns the banner actually
        gets (the terminal width less :data:`_TRANSCRIPT_INSET`), and never
        ragged. Falls back to the one-line name on terminals too narrow even for
        the portrait.

    The banner is centred by its consumer, so the art is returned flush-left and
    the returned rows are all the same width within a tier.
    """
    usable = (width or 80) - _TRANSCRIPT_INSET
    if usable >= LOCKUP_WIDTH:
        return "\n" + "\n".join(lockup()) + "\n"
    if usable >= PORTRAIT_WIDTH:
        return "\n" + "\n".join(compact_art()) + "\n"
    return f"\n{_NAME_LINE}\n"
