"""Entrance animations are skipped under load, never at the cost of visibility.

``_add_message`` animates EVERY chat message, and each animation repaints its
widget every frame for its duration. A fast stream stacked them — measured at 40
concurrent — turning the polish into jank: mounting cost 4.8 ms without the
animation and 10.0 ms with it.

The animation is now skipped once several are already in flight. The risk that
introduces is the serious one: a widget left at opacity 0 is invisible, which is
far worse than a slow fade. These pin both halves.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.color import Color as RichColor

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


def test_threshold_is_a_sane_size():
    """Small enough to protect a burst, large enough for an ordinary exchange."""
    from novacode_cli.tui.animations import _MAX_CONCURRENT_ENTRANCES

    assert 2 <= _MAX_CONCURRENT_ENTRANCES <= 20


def test_in_flight_counter_never_raises():
    """It reads a private Textual attribute; it must degrade, not explode."""
    from novacode_cli.tui.animations import _entrances_in_flight

    class _NoApp:
        @property
        def app(self):
            raise RuntimeError("no app")

    assert _entrances_in_flight(_NoApp()) == 0


async def _drive():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_tui_app import _FakeAgent, _SS
    from textual.widgets import Static

    from novacode_cli.tui.animations import animate_entrance
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    out: dict = {}
    app = NovaApp(
        agent=_FakeAgent(), assistant_id="nova-agent", session_state=_SS(),
        backend=None, token_tracker=TokenTracker(), image_tracker=None,
        model_name="m",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(6):
            await pilot.pause()
        await asyncio.sleep(0.5)  # let startup animations drain
        for _ in range(4):
            await pilot.pause()
        tr = app.query_one("#transcript")

        # Quiet UI: the polish is preserved (starts transparent, fades in).
        w = Static("quiet")
        await tr.mount(w)
        animate_entrance(w, "slide")
        out["idle_animates"] = float(w.styles.opacity) < 1.0
        await asyncio.sleep(0.45)
        for _ in range(3):
            await pilot.pause()
        out["idle_ends_visible"] = float(w.styles.opacity) == 1.0

        # Burst: past the threshold the animation is skipped — but NOTHING may
        # be left invisible, now or after every animation has settled.
        widgets = []
        for i in range(20):
            x = Static(f"burst {i}")
            await tr.mount(x)
            animate_entrance(x, "slide")
            widgets.append(x)
        # How many were shown WITHOUT waiting for any animation. Not asserted as
        # zero: the first few widgets are below the threshold and do animate, so
        # sampling here can legitimately catch one mid-fade at opacity 0 — under
        # full-suite load the animator drains slower and that is exactly what
        # happened. "Mid-fade" and "stuck invisible" are different things, and
        # only the second is a bug; `_MAX_CONCURRENT_ENTRANCES` bounds how many
        # can ever be mid-fade at once.
        out["shown_immediately"] = sum(
            1 for x in widgets if float(x.styles.opacity) > 0.0
        )
        out["mid_fade"] = len(widgets) - out["shown_immediately"]

        # The real property: once every animation has settled, nothing is
        # invisible. A broken skip leaves widgets at 0 here forever.
        await asyncio.sleep(0.8)
        for _ in range(6):
            await pilot.pause()
        out["invisible_after_settle"] = sum(
            1 for x in widgets if float(x.styles.opacity) == 0.0
        )
        out["all_visible"] = all(float(x.styles.opacity) == 1.0 for x in widgets)
    return out


def test_animation_is_skipped_under_load_but_nothing_stays_invisible():
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive())
    assert out["idle_animates"], "a quiet UI should still animate (polish preserved)"
    assert out["idle_ends_visible"], "the idle fade must complete"
    # The skip must actually engage: most of a 20-widget burst appears at once
    # rather than every one queueing an animation. Bounded by the threshold, so
    # this stays true regardless of how slowly the animator drains.
    from novacode_cli.tui.animations import _MAX_CONCURRENT_ENTRANCES

    assert out["mid_fade"] <= _MAX_CONCURRENT_ENTRANCES + 2, (
        f"{out['mid_fade']} widgets were animating — the skip did not engage"
    )
    # The one that matters: nothing may stay invisible once animations settle.
    assert out["invisible_after_settle"] == 0, (
        "a skipped animation left a widget at opacity 0 — invisible content"
    )
    assert out["all_visible"], "every burst widget must end fully opaque"


# ---------------------------------------------------------------------------
# What the fade must not leave behind
# ---------------------------------------------------------------------------


def _rgb(color: RichColor) -> tuple[int, int, int]:
    """RGB channels of a Rich colour, truecolor or otherwise.

    ``rich_style.color``/``bgcolor`` are Rich ``Color`` values, not Textual ones,
    and a non-truecolor one has no ``triplet``.
    """
    triplet = getattr(color, "triplet", None)
    if triplet is not None:
        return (triplet.red, triplet.green, triplet.blue)
    get_truecolor = getattr(color, "get_truecolor", None)
    if get_truecolor is not None:
        return tuple(get_truecolor())[:3]
    return (0, 0, 0)


def _relative_luminance(color: RichColor) -> float:
    """WCAG relative luminance of a Rich colour (0..1)."""

    def linear(component: float) -> float:
        value = component / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in _rgb(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(foreground: RichColor, background: RichColor) -> float:
    """WCAG contrast ratio between two Textual ``Color`` values."""
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _modal_button_contrast() -> float:
    """Fade a modal in, then report the contrast of its variant button's label.

    Returns the ratio measured once the fade has finished and the repair has had
    its grace frames, so the value reflects what the user is left looking at.
    """

    async def drive() -> float:
        from textual.app import App, ComposeResult
        from textual.containers import Vertical
        from textual.screen import ModalScreen
        from textual.widgets import Button

        from novacode_cli.tui.animations import animate_modal_screen
        from novacode_cli.tui.widgets import NOVA_TOKYO_NIGHT

        class _Host(App):
            def compose(self) -> ComposeResult:
                return []

        class _Modal(ModalScreen[None]):
            def compose(self) -> ComposeResult:
                with Vertical(id="modal-box"):
                    yield Button("Switch", id="switch", variant="success")

            def on_mount(self) -> None:
                animate_modal_screen(self)

        app = _Host()
        app.register_theme(NOVA_TOKYO_NIGHT)
        app.theme = "tokyo-night"

        ratio = 0.0
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(_Modal())

            # Let the fade finish: the bug appears only once opacity is back to
            # 1, because the descendant keeps the blend it cached on the way up.
            for _ in range(240):
                await pilot.pause()
                boxes = list(app.screen.query("#modal-box"))
                if boxes and float(boxes[0].styles.opacity) >= 1.0:
                    break

            # ...then give the repair its grace frames before measuring.
            for _ in range(60):
                await pilot.pause()
                button = app.screen.query_one("#switch", Button)
                style = button.visual_style.rich_style
                if style.color is None or style.bgcolor is None:
                    continue
                ratio = _contrast_ratio(style.color, style.bgcolor)
                if ratio >= 3.0:
                    break
        return ratio

    return asyncio.run(drive())


def test_a_faded_in_modal_leaves_its_buttons_readable():
    """A fade must not leave a variant button painting the screen behind its label.

    ``visual_style`` bakes every ancestor's opacity into the value it caches, so a
    fade that starts at 0 left the buttons painting the screen background for
    good: a ``success`` Button's auto-contrast label, chosen for its light fill,
    arrived dark on dark — an invisible "Switch".
    """
    if not _HAS_TEXTUAL:
        return
    ratio = _modal_button_contrast()
    assert ratio >= 3.0, (
        f"the button label is unreadable on its own background (contrast {ratio:.2f}:1)"
    )


# ---------------------------------------------------------------------------
# ...and the frames the fade passes through on the way there
# ---------------------------------------------------------------------------


def _modal_fade_samples() -> list[tuple[float, float, tuple[int, int, int], tuple[int, int, int]]]:
    """Sample a modal fade every frame: ``(opacity, contrast, label fg, label bg)``.

    Fading the modal in is the only way to catch the frames the end-state test
    above cannot see. The repair used to run once, after the fade, so every frame
    *before* it painted the transparent blend the descendant had cached at
    ``opacity: 0`` — the "black stripe" a ``/model`` screenshot showed.
    """

    async def drive():
        from textual.app import App, ComposeResult
        from textual.containers import Vertical
        from textual.screen import ModalScreen
        from textual.widgets import Button

        from novacode_cli.tui.animations import animate_modal_screen
        from novacode_cli.tui.widgets import NOVA_TOKYO_NIGHT

        class _Host(App):
            def compose(self) -> ComposeResult:
                return []

        class _Modal(ModalScreen[None]):
            def compose(self) -> ComposeResult:
                with Vertical(id="modal-box"):
                    yield Button("Switch", id="switch", variant="success")

            def on_mount(self) -> None:
                animate_modal_screen(self)

        app = _Host()
        app.register_theme(NOVA_TOKYO_NIGHT)
        app.theme = "tokyo-night"

        samples = []
        async with app.run_test(size=(120, 24)) as pilot:
            app.push_screen(_Modal())
            for _ in range(400):
                await asyncio.sleep(0.005)
                await pilot.pause()
                boxes = list(app.screen.query("#modal-box"))
                buttons = list(app.screen.query("#switch"))
                if not (boxes and buttons):
                    continue
                opacity = float(boxes[0].styles.opacity)
                style = buttons[0].visual_style.rich_style
                if style.color is None or style.bgcolor is None:
                    continue
                samples.append(
                    (
                        opacity,
                        _contrast_ratio(style.color, style.bgcolor),
                        _rgb(style.color),
                        _rgb(style.bgcolor),
                    )
                )
        return samples

    return asyncio.run(drive())


def test_the_fade_paints_the_buttons_own_fill_on_every_frame():
    """Every frame of the fade must show the button's fill, not the screen behind it.

    Two properties, because either alone can be satisfied by a broken fade: the
    painted fill has to *move* with the opacity (a stale cache holds one value for
    the whole fade while the opacity rises), and the label must clear 3:1 once the
    modal is more than half way in.

    The readability bar starts at half opacity on purpose. Measured on a correct
    fade, the ratio is 2.3:1 at 0.31, 3.3:1 at 0.56 and 10.5:1 settled — a frame at
    a third opacity is a modal that is barely on screen, and every colour in it is
    dimmed by design. Asserting 3:1 there would fail a fade that is working. The
    bug this pins measured 1.1:1 at *every* opacity, including fully faded in.
    """
    if not _HAS_TEXTUAL:
        return
    samples = _modal_fade_samples()
    mid = [s for s in samples if 0.25 <= s[0] < 1.0]
    assert len(mid) >= 3, f"only {len(mid)} frames landed inside the fade: {samples[:6]}"

    fills = {sample[3] for sample in mid}
    assert len(fills) > 1, (
        "the button's painted fill never moved while the opacity rose — it is "
        f"painting a stale (transparent) blend: {[(round(s[0], 2), s[3]) for s in mid]}"
    )

    dim = [sample for sample in samples if sample[0] >= 0.5]
    unreadable = [(round(o, 2), round(c, 2)) for o, c, _fg, _bg in dim if c < 3.0]
    assert not unreadable, f"the label is unreadable at opacity >= 0.5: {unreadable}"


def test_a_skipped_entrance_still_clears_a_stale_paint():
    """Fades are dropped under load; a stale blend must be dropped with them.

    The skip exists so a burst does not stack animations — not so that a widget
    keeps whatever it cached while an ancestor was transparent, which is the state
    that has no other way back (nothing bumps a descendant's cache key).
    """
    if not _HAS_TEXTUAL:
        return

    async def drive():
        from textual.app import App, ComposeResult
        from textual.containers import Vertical
        from textual.widgets import Button

        from novacode_cli.tui import animations

        class _Host(App):
            def compose(self) -> ComposeResult:
                with Vertical(id="modal-box"):
                    yield Button("Switch", id="switch", variant="success")

        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            box = app.query_one("#modal-box")
            button = app.query_one("#switch", Button)
            for _ in range(60):
                await pilot.pause()
                if button._visual_style is not None:
                    break
            painted = button._visual_style is not None

            original = animations._entrances_in_flight
            animations._entrances_in_flight = lambda _widget: 99
            try:
                animations.animate_entrance(box, "zoom")
                skipped = float(box.styles.opacity) == 1.0
                cleared = button._visual_style is None
            finally:
                animations._entrances_in_flight = original
        return painted, skipped, cleared

    painted, skipped, cleared = asyncio.run(drive())
    assert painted, "the button never painted, so there was no cache to go stale"
    assert skipped, "the load skip did not engage"
    assert cleared, "a skipped entrance left the stale paint in place"
