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


def test_wide_art_is_uniform_and_narrow_art_uses_compact_mark():
    wide = brand.art_for(200)
    lines = wide.splitlines()
    word_rows = [ln for ln in lines if ln.strip()]
    # The first six non-blank rows are the wordmark, padding included.
    assert word_rows[:6] == list(brand.WORDMARK_LINES)
    assert brand.compact_mark() in brand.art_for(40)
    assert brand.compact_mark() not in brand.art_for(200)


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


def test_rail_reaches_100_percent_when_all_phases_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old bar could never reach 100%: 15 declared steps vs ~6 emitted."""
    out = _render_rail(monkeypatch, _boot_messages(["ok"] * 6))
    assert "100%" in out, out


def test_rail_is_proportional_to_observed_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """2 of 3 observed phases done -> 67%, not a fraction of a guessed total."""
    out = _render_rail(monkeypatch, _boot_messages(["ok", "ok", "info"]))
    assert "67%" in out, out
    assert "100%" not in out, out


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
