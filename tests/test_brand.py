"""Nova's startup branding: one wordmark, one accent, honest progress.

These pin the defects the redesign fixed:

* the art was copy-pasted in four places with ragged right edges (rows of 29,
  44, 50 and 77 columns in one logo),
* the boot splash drew red while the TUI opened blue (and the rain logo fell
  back to a third green),
* the boot progress bar declared ``len(_BOOT_MESSAGES)`` steps (15) but a real
  boot emits ~6 status calls, so it died near 33% and never reached 100%.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli import brand

if TYPE_CHECKING:
    import pytest

# ── the art ─────────────────────────────────────────────────────────────────


def test_wordmark_rows_are_uniform_width():
    """The old art was ragged (29/44/50/77 cols). Every row must align."""
    widths = {len(line) for line in brand.WORDMARK_LINES}
    assert len(widths) == 1, f"ragged wordmark rows: {sorted(widths)}"
    assert widths.pop() == brand.WORDMARK_WIDTH


def test_wordmark_row_count_and_nonblank():
    assert len(brand.WORDMARK_LINES) == 6
    assert all(line.strip() for line in brand.WORDMARK_LINES)


def test_art_is_never_empty_and_never_ragged():
    for width in (0, 1, 40, 59, 60, 80, 120, 200, 500):
        art = brand.art_for(width)
        assert art.strip(), f"empty art at width {width}"
        rows = [r for r in art.splitlines() if r.strip()]
        assert rows, f"no non-blank rows at width {width}"


def test_wide_art_is_the_lockup_and_narrow_art_uses_the_compact_mark():
    """The wide tier is the lockup: portrait and wordmark side by side.

    The wordmark is no longer flush at row 0 -- the portrait sits to its left and
    the caption beneath it -- so this pins the *composition* rather than reading
    the wordmark off the top of the art, which would have made the two blocks
    indistinguishable from a wordmark-only design.
    """
    wide = brand.art_for(200)
    rows = [ln for ln in wide.splitlines() if ln.strip()]
    assert len(rows) == len(brand.lockup()), "the wide tier is not the lockup"
    assert rows == list(brand.lockup())

    # The portrait is kept, and the wordmark sits to its right on the shared rows.
    box = "\u2588\u2557\u2554\u255d\u255a"
    for row in rows[:6]:
        assert any("\u2800" <= ch <= "\u28ff" for ch in row), "portrait missing"
        portrait_end = max(i for i, ch in enumerate(row) if "\u2800" <= ch <= "\u28ff")
        assert any(ch in box for ch in row[portrait_end:]), "wordmark missing"

    # Below the lockup's width the portrait survives; the one-line mark is last.
    assert brand.compact_mark() not in brand.art_for(200)
    assert brand.compact_mark() in brand.art_for(20)
    narrow = brand.art_for(50)
    assert any("\u2800" <= ch <= "\u28ff" for ch in narrow), "portrait dropped at 50 cols"


def test_art_always_contains_a_literal_nova():
    """The block art spells NOVA in box glyphs, so the *text* must carry it too.

    ``tests/test_tui_app.py`` asserts ``"NOVA" in get_responsive_ascii(...)``;
    that only holds because the caption is a real word.
    """
    for width in (20, 40, 59, 60, 80, 200):
        assert "NOVA" in brand.art_for(width)


def test_tagline_line_appends_version_only_when_given():
    assert brand.tagline_line() == f"NOVA · {brand.TAGLINE}"
    assert brand.tagline_line("9.9.9").endswith("v9.9.9")


# ── the accent ──────────────────────────────────────────────────────────────


def test_accent_table_matches_the_real_textual_themes():
    """brand.py cannot import the themes (they need Textual), so it mirrors them.

    This is the guard on that duplication: if a theme's ``primary`` changes,
    the boot screen would silently keep tinting for the old colour.
    """
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT

    for theme in (NOVA_TOKYO_NIGHT, NOVA_MATRIX):
        declared = brand.THEME_ACCENTS.get(theme.name)
        assert declared is not None, f"{theme.name} missing from THEME_ACCENTS"
        assert declared.lower() == str(theme.primary).lower(), theme.name


def test_default_theme_is_registered():
    assert brand.DEFAULT_THEME in brand.THEME_ACCENTS
    assert brand.THEME_ACCENTS[brand.DEFAULT_THEME] == brand.FALLBACK_ACCENT


def _config_stub(theme_name: str, *, raises: bool = False) -> type:
    """A stand-in for ``NovaConfig`` whose ``get('theme')`` returns *theme_name*."""

    class _Stub:
        def get(self, _key: str, _default: object = None) -> str:
            if raises:
                raise RuntimeError
            return theme_name

    return _Stub


def test_get_accent_follows_the_persisted_theme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _config_stub("matrix"))
    assert brand.get_accent_hex() == brand.THEME_ACCENTS["matrix"]


def test_get_accent_falls_back_on_broken_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _config_stub("", raises=True))
    assert brand.get_accent_hex() == brand.FALLBACK_ACCENT


def test_get_accent_falls_back_on_unknown_theme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig", _config_stub("does-not-exist")
    )
    assert brand.get_accent_hex() == brand.FALLBACK_ACCENT


# ── the boot rail must not lie ──────────────────────────────────────────────


def _boot_messages(levels: list[str]) -> list[tuple[str, str]]:
    return [(f"phase{i}: detail{i}", lvl) for i, lvl in enumerate(levels)]


def _render_rail(monkeypatch: pytest.MonkeyPatch, messages: list[tuple[str, str]]) -> str:
    """Run BootAnimation._refresh() with a fake Live and return the drawn text."""
    import io

    from rich.console import Console

    import novacode_cli.config.config as nova_config
    from novacode_cli.config.config import BootAnimation

    sink = io.StringIO()
    console = Console(record=True, width=100, force_terminal=True, file=sink, color_system=None)
    captured: list = []
    monkeypatch.setattr(nova_config, "console", console)
    monkeypatch.setattr(
        BootAnimation,
        "_live",
        type("L", (), {"update": staticmethod(captured.append)})(),
    )
    monkeypatch.setattr(BootAnimation, "_messages", messages)
    monkeypatch.setattr(BootAnimation, "_start_time", 0.0)
    BootAnimation._refresh()
    assert captured, "no layout was produced"
    console.print(captured[-1])
    return sink.getvalue()


def test_rail_shows_no_percentage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fraction is not knowable here, so the rail must never render one.

    The previous rail showed ``done/total`` and, because only ``core_agent``
    emits an unconditional status, that was 0/1 -> a rail pinned at 0% for the
    whole boot. Any percentage is a lie; the rail reports activity instead.
    """
    for levels in (["info"], ["ok"] * 6, ["ok", "ok", "info"]):
        out = _render_rail(monkeypatch, _boot_messages(levels))
        assert "%" not in out, out


def test_rail_animates_without_any_status_call() -> None:
    """The rail must sweep during a silent phase (MCP discovery blocks for seconds).

    This is the regression that the old tests missed: they only ever checked
    frames built from synthetic multi-phase input, never the single-message
    frame a real boot actually shows.
    """
    from novacode_cli.config.config import BootRail

    rail = BootRail(accent="cyan", start=0.0)
    frames = {rail.frame(t).plain for t in (0.0, 0.2, 0.4, 0.6, 0.8)}
    assert len(frames) > 1, frames
    assert all(len(f) == BootRail.WIDTH for f in frames), frames


def test_rail_sweep_wraps_and_is_bounded() -> None:
    """The highlight stays inside the track at every sampled instant."""
    from novacode_cli.config.config import BootRail

    rail = BootRail(accent="cyan", start=0.0)
    for t in [i * 0.05 for i in range(400)]:
        plain = rail.frame(t).plain
        assert len(plain) == BootRail.WIDTH, (t, plain)


def test_finished_boot_shows_checks_and_no_spinner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed phase must not keep a spinner: that reads as 'still working'."""
    layout = _render_rail(monkeypatch, _boot_messages(["ok"] * 3))
    assert layout.count("✓") == 3, layout


def test_no_declared_total_steps_parameter():
    """The lying denominator is gone: neither entry point takes total_steps."""
    import inspect

    from novacode_cli.config.config import BootAnimation

    for fn in (BootAnimation.start, BootAnimation.async_start):
        params = inspect.signature(fn.__wrapped__).parameters
        assert "total_steps" not in params
    assert not hasattr(BootAnimation, "_total_steps")
