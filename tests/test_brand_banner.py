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
WIDTHS = [200, 160, 120, 100, 90, 82, 80, 78, 77, 76, 60, 59, 50]


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


def test_too_narrow_for_anything_yields_the_compact_mark() -> None:
    """A one-line mark is better than art that cannot fit.

    Below the portrait's own width the art gives up entirely and defers to
    ``brand.compact_mark()`` -- the single-line mark shared with the command
    banner and the Cowork UI.
    """
    rows = _rows(get_responsive_ascii(width=20))
    assert rows == [brand.compact_mark()]


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


@pytest.mark.parametrize("width", [200, 150, 120, 100, 80])
def test_rain_falls_on_the_art_rows_where_the_art_has_no_ink(width: int) -> None:
    """Rain must run *behind* the lockup, not stop at the portrait's edge.

    Regression for the removed confinement: the rain used to be skipped for
    every row of the art's bounding box. On an 80-column terminal that box is 74
    of the 76 drawn columns, so the backdrop had a full-height hole and the rain
    looked like it began after the wordmark. Skipping bought nothing either -- an
    inked cell is overwritten by the art whatever the rain buffer holds.

    The *uninked* columns of the art's own rows are the observation window: a
    bounding-box confinement blanks them, drawing the rain leaves them wet.
    """
    mw = _built(width)
    katakana = set(MatrixRain.KATAKANA)

    # The art's ink, derived from the art strings rather than from the widget,
    # so this does not merely re-assert the implementation.
    inked: dict[int, set[int]] = {}
    for ay, line in enumerate(mw._art_lines):
        cols: set[int] = set()
        gx = mw._art_left
        for ch in line:
            if ch not in ("\ufe0e", "\ufe0f") and ch != " " and 0 <= gx < mw._col_count:
                cols.add(gx)
            gx += cell_len(ch)
        inked[ay] = cols

    gaps = [
        (row_off, col)
        for row_off, cols in inked.items()
        for col in range(mw._art_left, mw._art_left + mw._art_w)
        if col not in cols
    ]
    assert gaps, "the art leaves no uninked columns to observe"

    wet = 0
    for _ in range(_FRAMES):
        mw._build_strips()
        for row_off, col in gaps:
            if mw._frame_lines[mw._art_top + row_off][col] in katakana:
                wet += 1
    assert wet > 0, (
        f"no rain on any uninked cell of the art's rows at width {width}: the art's "
        "bounding box is being skipped instead of just its ink"
    )


def test_the_art_overwrites_rain_on_its_own_cells() -> None:
    """The property that makes the confinement unnecessary, pinned directly.

    Every inked cell is composited after the rain, so the frame's *visible* text
    at an inked cell is the glyph, not katakana -- regardless of what the rain
    buffer holds. If that ever stops holding, the confinement becomes load
    bearing again and this test says so.
    """
    art = "AB " + " " * 5
    mw = _built(120, art=art)
    katakana = set(MatrixRain.KATAKANA)
    # Force rain to be present on every row, across the whole art width.
    for d in mw._columns:
        d["pos"] = 0.0
        d["speed"] = 0.0
        d["trail"] = 0
    mw._build_strips()
    row = mw._frame_lines[mw._art_top]
    left = mw._art_left
    assert row[left] == "A"
    assert row[left + 1] == "B"
    # The gaps beside the glyphs may rain; the glyphs never become katakana.
    assert "A" not in katakana
    assert "B" not in katakana
    assert not any(ch in katakana for ch in (row[left], row[left + 1]))


@pytest.mark.parametrize("width", [80, 100, 120, 200])
def test_the_banner_opens_with_rain_already_on_screen(width: int) -> None:
    """The home banner must not open empty.

    ``_init_columns`` used to seed every head at ``random.uniform(-span, 0)``,
    i.e. above the grid, so the first frame measured ~0.2% coverage: the window
    looked blank and the rain only crept in over the following ~15 seconds.
    Seeding across the grid puts a fifth of it on screen immediately.

    Deliberately a floor, not an exact figure: the coverage is random, and the
    defect being guarded is "nothing is falling yet", which is orders of
    magnitude away from the threshold.
    """
    art = get_responsive_ascii(width=width)
    mw = MatrixRain(art=art, width=width)
    mw._init_columns()
    mw._build_strips()
    hits = sum(
        1 for y in range(mw._row_count) for ch in mw._frame_lines[y] if ch in MatrixRain.KATAKANA
    )
    coverage = 100.0 * hits / (mw._col_count * mw._row_count)
    assert coverage > 5.0, f"the banner opened with only {coverage:.1f}% rain on screen"


def test_column_seeding_recycles_from_above() -> None:
    """Seeding in flight must not stop recycled columns falling in from the top.

    Otherwise the fall becomes a loop that never crosses the top edge.
    """
    mw = _built(120)
    for d in mw._columns:
        d["pos"] = mw._row_count + d["trail"] + 1  # past the bottom
    mw._fill_buffers()
    assert all(d["pos"] < 0 for d in mw._columns), (
        "a recycled column did not restart above the grid"
    )


@pytest.mark.parametrize("width", [200, 150, 120, 100, 80, 60, 50])
def test_every_column_draws_rain_in_a_single_driven_frame(width: int) -> None:
    """One frame, every column: the rain is not confined to the margins.

    A deterministic companion to the sampling test above. Waiting for random
    falls is flaky exactly where it matters most -- an 80-column terminal has
    only a handful of margin columns -- so this drives every column's head to a
    known row and asserts each one drew, including the columns the art covers.
    """
    mw = _built(width)
    assert 0 < mw._art_w < mw._col_count, "no margin columns beside the lockup"
    for d in mw._columns:
        d["pos"] = 0.0
        d["speed"] = 0.0
        d["trail"] = 0
    mw._build_strips()

    katakana = set(MatrixRain.KATAKANA)
    # Row 0 is above the art (`_art_top` is 2), so nothing is composited over it.
    dry = [col for col in range(mw._col_count) if mw._frame_lines[0][col] not in katakana]
    assert not dry, f"columns {dry[:8]} drew no rain at width {width}"


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
