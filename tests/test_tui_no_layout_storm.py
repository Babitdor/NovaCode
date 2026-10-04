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


# ── a finished answer's Markdown is rendered once per width ──────────────────


_ANSWER = (
    "## Findings\n\nSome **bold** text and `code`.\n\n- one\n- two\n\n"
    "```python\nfor i in range(3):\n    print(i)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
) * 4


def _print(renderable, width: int) -> str:
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=width, force_terminal=True, color_system="truecolor")
    console.print(renderable)
    return console.file.getvalue()


def test_cached_markdown_renders_identically_to_rich():
    from rich.markdown import Markdown

    from novacode_cli.tui.widgets import CachedMarkdown

    cached = CachedMarkdown(_ANSWER)
    assert isinstance(cached, Markdown), "copy/selection code checks isinstance(Markdown)"
    assert cached.markup == _ANSWER
    for width in (60, 100, 140):
        assert _print(cached, width) == _print(Markdown(_ANSWER), width), width


def test_cached_markdown_renders_once_per_width(monkeypatch):
    """Textual measures, then paints, then repaints: all at the same width."""
    from novacode_cli.tui import widgets

    renders: list[int] = []
    real = widgets._UncachedMarkdown.__rich_console__

    def counting(self, console, options):  # noqa: ANN001, ANN202
        renders.append(options.max_width)
        return real(self, console, options)

    monkeypatch.setattr(widgets._UncachedMarkdown, "__rich_console__", counting)
    cached = widgets.CachedMarkdown(_ANSWER)
    for _ in range(5):
        _print(cached, 100)
    assert renders == [100], "five paints at one width must parse and highlight once"
    _print(cached, 80)
    assert renders == [100, 80], "a resize re-renders, as it must"


# ── numpy must not reserve a buffer per CPU thread ───────────────────────────


def test_importing_nova_limits_openblas_threads_unless_already_set():
    """380 MB -> 9 MB of private memory per Nova process on a 12-thread machine."""
    import os
    import subprocess

    code = "import os, novacode_cli; print(os.environ.get('OPENBLAS_NUM_THREADS'))"
    env = {k: v for k, v in os.environ.items() if k != "OPENBLAS_NUM_THREADS"}
    run = lambda e: subprocess.run(  # noqa: E731
        [sys.executable, "-c", code], env=e, capture_output=True, text=True, timeout=120
    ).stdout.strip()
    assert run(env) == "1"
    assert run({**env, "OPENBLAS_NUM_THREADS": "4"}) == "4", "an explicit setting must win"


def test_an_app_focus_change_does_not_restyle_the_whole_screen():
    """Alt-tab used to re-apply the stylesheet to every widget (5.4 s freeze)."""
    import asyncio

    from novacode_cli.tui.app import NovaApp

    async def drive() -> tuple[int, bool]:
        app = NovaApp.__new__(NovaApp)
        calls = 0

        class _Screen:
            focused = None

            def update_node_styles(self, animate: bool = True) -> None:
                nonlocal calls
                calls += 1

            def set_focus(self, *a, **k) -> None:  # noqa: ANN002, ANN003
                pass

        screen = _Screen()
        type(app).screen = property(lambda self: screen)  # type: ignore[assignment]
        try:
            app._last_focused_on_app_blur = None
            app._watch_app_focus(False)
            app._watch_app_focus(True)
        finally:
            del type(app).screen
        return calls, "update_node_styles" in vars(screen)

    calls, shadow_left = asyncio.run(drive())
    assert calls == 0, "the whole-screen restyle must be skipped"
    assert not shadow_left, "the shadow must be removed so later restyles still work"


def test_plugin_commands_are_discovered_off_the_ui_thread(monkeypatch):
    """Discovery scans installed packages and reads files: 2-3 s on a busy disk."""
    import threading

    from novacode_cli.tui.app import NovaApp

    app = NovaApp.__new__(NovaApp)
    seen: dict = {}
    done = threading.Event()

    def discover(self):  # noqa: ANN001, ANN202
        seen["thread"] = threading.current_thread().name
        return {"weather": object()}

    def call_from_thread(self, fn, *a):  # noqa: ANN001, ANN002, ANN202
        fn(*a)
        done.set()

    monkeypatch.setattr(NovaApp, "_discover_plugin_commands", discover)
    monkeypatch.setattr(NovaApp, "call_from_thread", call_from_thread)
    monkeypatch.setattr(NovaApp, "is_running", property(lambda self: True))
    app._load_plugin_commands()
    assert done.wait(5)
    assert seen["thread"] == "nova-plugin-commands"
    assert "weather" in app._plugin_commands


def test_focus_returns_to_the_prompt_after_the_window_regains_focus():
    """The trimmed focus watcher must keep Textual's own bookkeeping intact."""
    import asyncio

    import test_tui_app as T

    from novacode_cli.tui.app import NovaApp, PromptInput
    from novacode_cli.ui.ui_elements import TokenTracker

    async def drive() -> tuple[bool, bool, bool]:
        app = NovaApp(
            agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
            token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", PromptInput)
            before = app.screen.focused is prompt
            app.app_focus = False
            await pilot.pause()
            blurred = app.screen.focused is None
            app.app_focus = True
            await pilot.pause()
            return before, blurred, app.screen.focused is prompt

    assert asyncio.run(drive()) == (True, True, True)
