"""Guards for the startup banner lockup and the rain confinement.

The banner art lives in ``novacode_cli.brand`` and is served by every startup
surface. The properties pinned here are exactly the ones the shipped art broke:

* every row of the art is the same width (the old art ranged 26-79 cells, which
  is why the portrait read as torn down its right side),
* no tier returns art wider than the columns the banner actually gets (at 80
  columns the old art was 77 wide against 76 usable, so it wrapped and jittered),
* the compositing and the rain confinement agree with Rich's idea of width.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from novacode_cli import brand
from novacode_cli.config.config import format_version_banner, get_responsive_ascii
from novacode_cli.tui.app import MatrixRain

#: Every width the banner can be asked for, plus the tier boundaries.
#: ``_MIN_WIDTH`` in ``tui/app.py`` is 50 -- below that the app shows a
#: "terminal too small" notice instead of a layout.
WIDTHS = [200, 160, 120, 100, 90, 80, 76, 75, 74, 60, 59, 50]


def _rows(art: str) -> list[str]:
    """The art's non-empty rows."""
    return [line for line in art.split("\n") if line]


# ── art geometry ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", WIDTHS)
def test_every_row_of_the_art_is_the_same_width(width: int) -> None:
    """Ragged rows are what made the old logo look torn on its right edge."""
    rows = _rows(get_responsive_ascii(width=width))
    assert rows, f"no art at width {width}"
    widths = {cell_len(row) for row in rows}
    assert len(widths) == 1, f"ragged rows at width {width}: {sorted(widths)}"


@pytest.mark.parametrize("width", WIDTHS)
def test_art_never_exceeds_the_columns_the_banner_gets(width: int) -> None:
    """The transcript insets its content by 2 cells each side.

    Art sized to the full terminal width overflows that inset and wraps, which
    pushes the logo down a row and lets it spring back as the rain shifts.
    """
    usable = width - 4
    widest = max(cell_len(row) for row in _rows(get_responsive_ascii(width=width)))
    assert widest <= usable, f"art {widest} wide into {usable} usable columns at width {width}"


@pytest.mark.parametrize("width", WIDTHS)
def test_art_rows_match_cell_width_in_codepoints_too(width: int) -> None:
    """The compositor indexes art per *codepoint*, into a per-*cell* grid.

    A character whose ``cell_len`` disagrees with ``len`` -- ``U+2665 U+FE0E``
    is two codepoints but one cell -- would silently consume an extra column
    when composited. ``cell_len``-based width checks cannot see that, so this
    pins the stronger property.
    """
    for row in _rows(get_responsive_ascii(width=width)):
        assert len(row) == cell_len(row), f"codepoint/cell mismatch in {row!r}"


def test_lockup_keeps_the_portrait_beside_the_wordmark() -> None:
    """Option B: the portrait is kept, and the two blocks sit side by side.

    Guards the composition, not just the width: a change that stacked the
    wordmark under the portrait would still pass the width checks.
    """
    rows = brand.lockup()
    assert cell_len(rows[0]) == brand.LOCKUP_WIDTH
    # The portrait's braille body is left of the wordmark's box glyphs on the
    # rows that carry both.
    both = [r for r in rows if max(r) >= "\u2800" and any(ch in r for ch in "\u2588\u2557\u2554")]
    assert both, "no row carries both the portrait and the wordmark"
    for row in both:
        portrait_end = max(i for i, ch in enumerate(row) if "\u2800" <= ch <= "\u28ff")
        box = "\u2588\u2557\u2554\u255d\u255a"
        wordmark_start = min(i for i, ch in enumerate(row) if ch in box)
        assert portrait_end < wordmark_start, f"blocks interleaved: {row!r}"


def test_lockup_has_the_tagline_beneath_the_wordmark() -> None:
    """The caption must sit in the right-hand column, not in the portrait."""
    rows = brand.lockup()
    caption_rows = [r for r in rows if "weakness" in r]
    assert len(caption_rows) == 1, "the caption line is missing from the lockup"
    caption = caption_rows[0]
    # The caption starts to the right of the portrait's 29-column field.
    assert caption.index("Everything") >= brand.PORTRAIT_WIDTH
    # ...and its row is still full width, so the edge stays straight.
    assert cell_len(caption) == brand.LOCKUP_WIDTH


def test_narrow_tiers_keep_the_portrait_and_fit() -> None:
    """Below the lockup's width the wordmark goes, but not the portrait."""
    art = get_responsive_ascii(width=59)
    rows = _rows(art)
    assert any(any("\u2800" <= ch <= "\u28ff" for ch in row) for row in rows), (
        "the portrait was dropped from the narrow tier"
    )
    assert max(cell_len(row) for row in rows) <= 59 - 4


def test_too_narrow_for_anything_yields_the_name_only() -> None:
    """A one-line mark is better than art that cannot fit."""
    rows = _rows(get_responsive_ascii(width=20))
    assert rows == ["\u2665 NOVA ~"]


def test_version_banner_carries_the_art_and_the_version() -> None:
    """``--version`` shares the lockup instead of keeping its own copy."""
    out = format_version_banner("9.9.9")
    assert "9.9.9" in out
    assert "NOVA" in out
    assert "\u2588\u2557" in out, "the block wordmark is missing"


# ── rain confinement ─────────────────────────────────────────────────────────

_FRAMES = 40


def _built(width: int, art: str | None = None) -> MatrixRain:
    """A mounted-and-ticking rain widget for *width*."""
    if art is None:
        art = get_responsive_ascii(width=width)
    mw = MatrixRain(art=art, width=width)
    mw._init_columns()
    return mw


@pytest.mark.parametrize("width", [200, 150, 120, 100, 80, 60, 50])
def test_rain_never_intrudes_on_the_lockup(width: int) -> None:
    """The point of the confinement: no katakana behind the lettering.

    Sampled over many frames, because the rain's column positions advance: a
    single frame could pass by luck.
    """
    mw = _built(width)
    katakana = set(MatrixRain.KATAKANA)
    band = range(mw._art_left, mw._art_left + mw._art_w)
    art_rows = range(mw._art_top, mw._art_top + len(mw._art_lines))
    assert mw._art_w > 0, "no art bounding box to confine against"
    for _ in range(_FRAMES):
        mw._build_strips()
        for y in art_rows:
            row = mw._frame_lines[y]
            intruders = [row[x] for x in band if row[x] in katakana]
            assert not intruders, (
                f"rain ({''.join(intruders)!r}) inside the art box at row {y}, width {width}"
            )


@pytest.mark.parametrize("width", [200, 150, 120, 100, 80, 60, 50])
def test_margin_columns_draw_rain_and_band_columns_do_not(width: int) -> None:
    """Confining the rain must not kill it.

    A guard on the confinement alone would pass if the rain were switched off
    entirely, so this pins both sides: every column outside the lockup's box
    draws, and every column inside it does not.

    The rain is driven to a known state rather than sampled over time. Waiting
    for random columns to fall is flaky exactly where it matters most -- at 80
    columns there are only 4 margin columns, and a frame may show none of them.
    ``pos=0`` and ``speed=0`` place every column's head on row 0, which is above
    the art, so a band column that (wrongly) drew would not be masked by the
    composite overwriting it.
    """
    mw = _built(width)
    # There must be margins at all, or the confinement has nothing to leave.
    assert 0 < mw._art_w < mw._col_count, "no margin columns beside the lockup"
    for d in mw._columns:
        d["pos"] = 0.0
        d["speed"] = 0.0
        d["trail"] = 0
    mw._build_strips()

    katakana = set(MatrixRain.KATAKANA)
    for col in range(mw._col_count):
        drew = mw._frame_lines[0][col] in katakana
        in_band = mw._art_left <= col < mw._art_left + mw._art_w
        if in_band:
            assert not drew, f"column {col} is inside the lockup but drew rain"
        else:
            assert drew, f"margin column {col} drew no rain at width {width}"


def test_grid_never_overflows_the_transcript_width() -> None:
    """The widget box must fit the content width, or every row wraps."""
    mw = _built(80)
    assert mw._col_count <= 80 - 4


def test_zero_width_selector_does_not_shift_the_row() -> None:
    """A U+FE0E must not consume a cell when composited.

    The compositor builds a per-cell grid but walked the art per codepoint, so a
    two-codepoint/one-cell pair shifted everything after it one column right.
    """
    plain = "AB" + " " * 8
    decorated = "A\ufe0eB" + " " * 8
    assert cell_len(plain) == cell_len(decorated)

    mw_plain = _built(120, art=plain)
    mw_plain._build_strips()
    mw_deco = _built(120, art=decorated)
    mw_deco._build_strips()

    def row_with(mw: MatrixRain, ch: str) -> tuple[int, int]:
        for y, row in enumerate(mw._frame_lines):
            if ch in row:
                return y, row.index(ch)
        msg = f"{ch!r} was not composited"
        raise AssertionError(msg)

    y_a, x_a = row_with(mw_plain, "A")
    y_b, x_b = row_with(mw_plain, "B")
    y_a2, x_a2 = row_with(mw_deco, "A")
    y_b2, x_b2 = row_with(mw_deco, "B")
    assert (y_a, x_a) == (y_a2, x_a2)
    assert (y_b, x_b) == (y_b2, x_b2), "the selector shifted the following cell"
