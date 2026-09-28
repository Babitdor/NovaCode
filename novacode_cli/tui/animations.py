"""Cool animated effects for the Nova TUI.

Provides reusable animation sequences and helpers that add visual polish —
entrance transitions, pulse effects, shimmer bars, and breathing indicators.
All animations use Textual's native Python-side ``animate()`` API (TCSS does
not support ``@keyframes``).

Usage
-----
Call one of the helpers on *mount* (or right after *mount*) so the animation
fires when the widget appears:

    from textual.color import Color

    class MyWidget(Widget):
        def on_mount(self) -> None:
            animate_entrance(self, "slide")   # slide in from below
            animate_entrance(self, "fade")    # pure fade
            pulse_border(self)                # pulsing left-border glow
            pulse_soft(self)                  # gentle opacity breathing
"""

from __future__ import annotations

import math
from typing import Any

from textual.color import Color
from textual.widget import Widget

__all__ = [
    "animate_entrance",
    "animate_modal_screen",
    "pulse_border",
    "pulse_soft",
    "shimmer_bar",
    "glow_breathe",
    "stop_animation",
]

# ── Entrance animations ─────────────────────────────────────────────────────


#: Above this many entrance animations already running, new widgets appear
#: instantly. Chosen so an ordinary exchange (a handful of messages and tool
#: cards) still animates, while a burst does not pile on more work.
_MAX_CONCURRENT_ENTRANCES = 6

#: Extra delay past a fade's own duration before repairing its subtree, and how
#: many times to re-arm while waiting for the fade to actually finish (an
#: animation under load can overrun the duration it was given).
_FADE_REPAIR_MARGIN = 0.05
_FADE_REPAIR_ATTEMPTS = 12


def _refresh_cached_paints(root: Widget) -> None:
    """Drop *root*'s subtree cache of painted styles.

    ``Widget.visual_style`` bakes every ancestor's ``opacity`` into the value it
    caches, but it only recomputes when the widget's OWN style key changes. A fade
    that starts at ``opacity: 0`` therefore leaves descendants painting the blend
    they cached on the way up, and that blend is the plain screen background. A
    variant Button (``-success``, ``-primary``) then fills with the screen colour
    instead of its own, while its label — an auto-contrast colour chosen for the
    light fill — arrives dark on dark, i.e. invisible. Rebinding the cache once
    the fade has finished repaints them with their real colours.
    """
    for node in root.walk_children(with_self=True):
        # No guard needed: this only ever runs on a mounted subtree, and a node
        # without the attribute is skipped rather than touched.
        if getattr(node, "_visual_style", None) is not None:
            node._visual_style = None
            node.refresh()


def _repair_paints_after_fade(widget: Widget, duration: float, attempt: int = 1) -> None:
    """Re-cache *widget*'s subtree once the fade it just started is opaque.

    Re-arms itself while the fade is still running so the repair cannot land
    early and be undone by the frames that follow.
    """

    def repair() -> None:
        try:
            opaque = float(widget.styles.opacity) >= 1.0
        except Exception:  # noqa: BLE001 — unmounted mid-fade
            return
        if opaque or attempt >= _FADE_REPAIR_ATTEMPTS:
            _refresh_cached_paints(widget)
            return
        _repair_paints_after_fade(widget, duration, attempt + 1)

    widget.set_timer(duration + _FADE_REPAIR_MARGIN, repair)



def _entrances_in_flight(widget: Widget) -> int:
    """How many animations the app currently has scheduled (0 if unknown)."""
    try:
        return len(widget.app.animator._animations)
    except Exception:  # noqa: BLE001 — a private attr; never fail the mount
        return 0


def animate_entrance(widget: Widget, style: str = "slide") -> None:
    """Fade *widget* in as it first appears, using Textual's ``animate()``.

    Note: Textual 8.2.7 has **no ``scale`` style**, and animating ``offset``
    raises (``start_value must be float`` on a ``ScalarOffset``) — so a true
    zoom/slide transform isn't available. Opacity is the only entrance property
    that animates reliably here, so every style fades in; ``style`` only tunes
    the duration/easing so different surfaces (modals vs. messages) feel a
    little distinct.

    Styles
    ------
    ``"fade"``  — quick fade (0.25s).
    ``"slide"`` — slightly longer fade (0.32s).
    ``"zoom"``  — medium fade (0.28s).

    Skipped under load
    ------------------
    Animating costs real time: a mount goes from ~4.8 ms to ~10.0 ms, and each
    animation repaints its widget every frame for its whole duration. Because
    ``_add_message`` animates EVERY chat message, a fast stream stacks them —
    measured at 40 concurrent animations — and the polish becomes jank. When
    more than :data:`_MAX_CONCURRENT_ENTRANCES` are already running, the widget
    is shown immediately instead. A quiet UI still animates; a busy one stays
    responsive.

    The fade is also repaired on the way out (see
    :func:`_refresh_cached_paints`): descendants must not be left painting the
    background they cached while the widget was transparent.
    """
    duration = {"fade": 0.25, "slide": 0.32, "zoom": 0.28}.get(style, 0.28)
    if _entrances_in_flight(widget) > _MAX_CONCURRENT_ENTRANCES:
        # Under load: show it at once rather than adding another animation.
        widget.styles.opacity = 1.0
        return
    widget.styles.opacity = 0.0
    widget.styles.animate("opacity", 1.0, duration=duration, easing="out_cubic")
    _repair_paints_after_fade(widget, duration)


def animate_modal_screen(screen: Any) -> None:
    """Apply a zoom-in entrance to a ``ModalScreen``'s ``#modal-box``.

    Must be called from ``on_mount`` (the DOM is ready, and the animation
    fires before the user sees the modal).
    """
    try:
        box = screen.query_one("#modal-box")
        animate_entrance(box, "zoom")
    except Exception:  # noqa: BLE001
        pass


# ── Ongoing animated effects (call from on_mount, return a timer ref) ───────


def pulse_border(
    widget: Widget, color: Color | str | None = None, period: float = 1.2
) -> Any:
    """Make the left border of *widget* pulse with a breathing glow.

    Returns a ``set_interval`` timer handler that can be stopped via
    ``timer.stop()`` (or :func:`stop_animation`).
    """
    if color is None:
        try:
            color = widget.app.theme_colors.warning
        except Exception:  # noqa: BLE001
            color = Color.parse("#e0af68")

    peak_alpha = 1.0
    low_alpha = 0.4
    t = widget.set_interval(
        period,
        _BorderPulser(widget, color, peak_alpha, low_alpha),
    )
    return t


def pulse_soft(widget: Widget, period: float = 1.5) -> Any:
    """Gentle opacity breathing for status indicators.

    Returns a ``set_interval`` timer that can be stopped.
    """
    peak = 1.0
    trough = 0.45
    frame = [0]

    def _tick() -> None:
        frame[0] += 0.5
        normalized = (math.sin(frame[0] * math.pi) + 1) / 2
        alpha = trough + (peak - trough) * normalized
        widget.styles.opacity = alpha

    t = widget.set_interval(0.1, _tick)
    return t


def shimmer_bar(widget: Widget) -> Any:
    """Sweeping highlight effect for loading/progress bars.

    Animates the ``tint`` property back and forth over 2s.
    Returns a ``set_interval`` timer.
    """
    try:
        accent = widget.app.theme_colors.accent
    except Exception:  # noqa: BLE001
        accent = Color.parse("#bb9af7")

    forward = True
    intensity = [0.0]

    def _tick() -> None:
        nonlocal forward
        step = 0.08
        if forward:
            intensity[0] = min(intensity[0] + step, 0.35)
            if intensity[0] >= 0.35:
                forward = False
        else:
            intensity[0] = max(intensity[0] - step, 0.0)
            if intensity[0] <= 0.0:
                forward = True
        widget.styles.tint = accent.with_alpha(intensity[0])

    t = widget.set_interval(0.08, _tick)
    return t


def glow_breathe(widget: Widget, period: float = 2.5) -> Any:
    """Border glow that breathes gently.

    Uses the theme's accent color.
    Returns a ``set_interval`` timer.
    """
    try:
        accent = widget.app.theme_colors.accent
    except Exception:  # noqa: BLE001
        accent = Color.parse("#7aa2f7")

    peak = 0.35
    trough = 0.05

    def _tick() -> None:
        t_val = getattr(_tick, "_time", 0.0) + 0.08
        setattr(_tick, "_time", t_val)
        normalized = (math.sin(t_val * math.pi) + 1) / 2
        alpha = trough + (peak - trough) * normalized
        widget.styles.tint = accent.with_alpha(alpha)

    t_ref = widget.set_interval(0.08, _tick)
    return t_ref


def stop_animation(timer: Any) -> None:
    """Safely stop an animation timer created by one of the helpers above."""
    if timer is not None:
        try:
            timer.stop()
        except Exception:  # noqa: BLE001
            pass


# ── Internal helpers ─────────────────────────────────────────────────────────


class _BorderPulser:
    """Callable for ``set_interval`` that oscillates border opacity."""

    def __init__(
        self, widget: Widget, color: Color, peak: float, low: float
    ) -> None:
        self._widget = widget
        self._color = color
        self._peak = peak
        self._low = low
        self._frame = 0

    def __call__(self) -> None:
        self._frame += 1
        normalized = (math.sin(self._frame * math.pi / 2) + 1) / 2
        alpha = self._low + (self._peak - self._low) * normalized
        try:
            # Textual's border style takes a (type, color) tuple — a single
            # "thick #hex" string raises "too many values to unpack".
            self._widget.styles.border_left = (
                "thick",
                self._color.with_alpha(alpha).hex,
            )
        except Exception:  # noqa: BLE001
            pass
