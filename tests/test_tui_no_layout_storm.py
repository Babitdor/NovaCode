"""The live status tick must repaint, not re-lay-out.

``Static.update()`` defaults to ``layout=True``, and a layout pass re-arranges
the whole screen. The status line ticks 20x a second while a turn is live, so
with the default every frame cost two full reflows — on a long transcript that
is more than a second of work per second, and the UI stops responding. The
freeze watchdog attributed 1,000 of 1,822 frozen seconds to exactly this.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from rich.text import Text

sys.path.insert(0, "tests")


async def _layouts_during_ticks() -> list[str]:
    import test_tui_app as T
    from textual.widgets import Static

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    app = NovaApp(
        agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
    )
    layouts: list[str] = []
    real = Static.update

    def recording(self, content="", *, layout=True):  # noqa: ANN001, ANN202
        if layout:
            layouts.append(self.id or type(self).__name__)
        return real(self, content, layout=layout)

    async with app.run_test(size=(140, 40)) as pilot:
        app._log(Text("hello"))
        app._turn_active = True
        for _ in range(3):  # warm-up: first paint legitimately establishes sizes
            app._tick()
            await pilot.pause()
        Static.update = recording  # type: ignore[method-assign]
        try:
            for _ in range(10):
                app._tick()
        finally:
            Static.update = real  # type: ignore[method-assign]
    return layouts


def test_status_ticks_do_not_trigger_layout():
    pytest.importorskip("textual")
    layouts = asyncio.run(_layouts_during_ticks())
    # The elapsed-time text can gain a digit ("9.9s" -> "10.0s"); that one frame
    # may lay out. Twenty reflows for ten ticks is the regression.
    assert len(layouts) <= 1, f"status ticks forced layout passes: {layouts}"


def test_paint_only_lays_out_when_size_changes():
    from novacode_cli.tui.app import _paint

    calls: list[bool] = []

    class _W:
        def update(self, content, *, layout=True):  # noqa: ANN001, ANN202
            calls.append(layout)

    w = _W()
    _paint(w, Text("⏱ 12s"))   # first paint: layout
    _paint(w, Text("⏱ 13s"))   # same width: repaint only
    _paint(w, Text("⏱ 13s"))   # identical: skipped
    _paint(w, Text("⏱ 1m 02s"))  # wider: layout
    assert calls == [True, False, True]


def test_live_output_batches_are_trimmed_to_their_tail():
    from novacode_cli.tui.app import _LIVE_OUTPUT_MAX_LINES, _display_tail

    assert _display_tail("short\n") == "short\n"
    big = "".join(f"line {i}\n" for i in range(5_000))
    shown = _display_tail(big)
    assert shown.endswith("line 4999\n"), "the tail is what the user is watching"
    assert shown.count("\n") <= _LIVE_OUTPUT_MAX_LINES + 1
    assert "trimmed" in shown.splitlines()[0]


def test_paint_takes_the_safe_path_for_non_text():
    """A Group or Markdown's size cannot be read off its text."""
    from rich.console import Group

    from novacode_cli.tui.app import _paint

    calls: list[bool] = []

    class _W:
        def update(self, content, *, layout=True):  # noqa: ANN001, ANN202
            calls.append(layout)

    w = _W()
    _paint(w, Group(Text("a")))
    _paint(w, Group(Text("a"), Text("b")))
    assert calls == [True, True]


def test_paint_repaints_when_only_the_colour_changed():
    """Text.__eq__ ignores the base style; a theme switch must still repaint."""
    from novacode_cli.tui.app import _paint

    calls: list[bool] = []

    class _W:
        def update(self, content, *, layout=True):  # noqa: ANN001, ANN202
            calls.append(layout)

    w = _W()
    _paint(w, Text("main", style="#ff0000"))
    _paint(w, Text("main", style="#00ff00"))
    assert calls == [True, False], "recoloured: repaint, no layout"


async def _palette_calls(typed: str) -> int:
    import test_tui_app as T

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    app = NovaApp(
        agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
    )
    calls: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        app._update_palette = lambda line, col=None: calls.append(line)  # type: ignore[method-assign]
        app._feed_palette(typed, len(typed))
        await pilot.pause()
    return len(calls)


def test_plain_typing_does_not_run_completion():
    """Only a leading / or an @token can have candidates."""
    pytest.importorskip("textual")
    assert asyncio.run(_palette_calls("explain the context manager")) == 0
    assert asyncio.run(_palette_calls("/comp")) == 1
    assert asyncio.run(_palette_calls("look at @src/ma")) == 1


def test_gc_tuning_is_inert_under_pytest_and_rate_limits_idle_collections(monkeypatch):
    import gc

    from novacode_cli.tui import gc_tuning

    before = gc.get_threshold()
    assert gc_tuning.tune() is False, "process-global settings must not leak into the suite"
    assert gc.get_threshold() == before

    monkeypatch.setattr(gc_tuning, "_last_full", 0.0)
    monkeypatch.setattr(gc_tuning.time, "monotonic", lambda: 1_000.0)
    assert gc_tuning.collect_while_idle(busy=True) is False, "never during a turn"
    assert gc_tuning.collect_while_idle() is True
    assert gc_tuning.collect_while_idle() is False, "rate-limited"
