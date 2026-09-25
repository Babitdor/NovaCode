"""Live motion for Nova's status line and tool-group title.

Two effects, both cheap enough to repaint on the 20 Hz status tick:

``spinner(tick)``
    A single braille dot orbiting a 2x4 cell grid. One glyph, so the status text
    never shifts sideways as it animates — a multi-cell spinner (the half-block
    ``▖▘▙▚`` set this replaced) changes the label's x-position every frame, which
    reads as jitter rather than motion.

``shimmer(text, tick, pal)``
    A soft band of light travelling left to right across the text. This is the
    "something is happening" signal that carries the modern/assistant feel: the
    words stay legible and still, and only their brightness moves.

Both are pure functions of a tick counter — no timers, no widget access — so
they are trivially testable and cannot drift out of sync with the app's clock.

Colours come from the active theme's
:class:`~novacode_cli.tui.palette.FooterPalette`, so the motion re-tints with
``/theme``. Note the peak brightness blends toward the theme's *foreground*
rather than using the accent alone: under a muted palette (flexoki's accent is
``#9B76C8``) a band drawn only in the accent is nearly invisible against the
muted base.
"""

from __future__ import annotations

from rich.color import Color, blend_rgb
from rich.text import Text

from novacode_cli.tui.palette import FooterPalette

__all__ = [
    "SPINNER_FRAMES",
    "SPINNER_TICKS_PER_FRAME",
    "SHIMMER_SPEED",
    "SHIMMER_WIDTH",
    "shimmer",
    "spinner",
    "tool_group_title",
]

#: A single dot orbiting a braille cell. Every frame is one glyph wide, so the
#: text after it does not move.
SPINNER_FRAMES: tuple[str, ...] = (
    "\u280b",  # ⠋
    "\u2819",  # ⠙
    "\u2839",  # ⠹
    "\u2838",  # ⠸
    "\u283c",  # ⠼
    "\u2834",  # ⠴
    "\u2826",  # ⠦
    "\u2827",  # ⠧
    "\u2807",  # ⠇
    "\u280f",  # ⠏
)

#: Status ticks per spinner step. At the 20 Hz tick rate, 2 gives one full
#: revolution per second — fast enough to read as activity, slow enough not to
#: strobe.
SPINNER_TICKS_PER_FRAME = 2

#: Characters the shimmer head advances per status tick (~1.6 cols at 20 Hz).
SHIMMER_SPEED = 1.6

#: Half-width of the shimmer band, in characters. Wider = softer and calmer;
#: narrower = a tighter scanner look.
SHIMMER_WIDTH = 9.0


def spinner(tick: int) -> str:
    """The spinner glyph for *tick*.

    Args:
        tick: A monotonically increasing status-tick counter.

    Returns:
        One braille glyph.
    """
    step = tick // SPINNER_TICKS_PER_FRAME
    return SPINNER_FRAMES[step % len(SPINNER_FRAMES)]


def _blend(a: str, b: str, t: float) -> str:
    """Blend hex colour *a* toward *b* by *t* (0 = a, 1 = b)."""
    try:
        ca = Color.parse(a).get_truecolor()
        cb = Color.parse(b).get_truecolor()
    except Exception:  # noqa: BLE001 — a bad theme colour must not break a frame
        return a
    m = blend_rgb(ca, cb, max(0.0, min(1.0, t)))
    return f"#{m.red:02x}{m.green:02x}{m.blue:02x}"


def shimmer(
    text: str,
    tick: int,
    pal: FooterPalette,
    *,
    base: str | None = None,
    peak: str | None = None,
    speed: float = SHIMMER_SPEED,
    width: float = SHIMMER_WIDTH,
) -> Text:
    """Render *text* with a band of light sweeping across it.

    Each character is tinted by its distance from the travelling head, so the
    brightest few characters form a soft comet. The head runs off the right edge
    and re-enters from the left, giving the sweep a gap to breathe instead of a
    permanent bright seam.

    Args:
        text: The text to animate. Its plain content is never altered.
        tick: Status-tick counter driving the motion.
        pal: Active theme palette.
        base: Colour of the unlit text (default: the palette's muted tone).
        peak: Colour of the fully lit characters (default: the theme foreground
            nudged toward the accent, so the band stays visible on muted themes).
        speed: Characters the head advances per tick.
        width: Half-width of the band, in characters.

    Returns:
        A styled ``Text``. ``len(result.plain) == len(text)`` always.
    """
    if not text:
        return Text()

    base = base or pal.muted
    peak = peak or _blend(pal.text, pal.accent, 0.45)

    # The head travels one full text-length plus a band-width of lead-in/out, so
    # there is a clear pause between passes.
    period = len(text) + 2 * width
    head = (tick * speed) % period - width

    out = Text()
    for index, char in enumerate(text):
        distance = abs(index - head)
        if distance >= width:
            out.append(char, style=base)
            continue
        # Squared falloff: a tight core with a soft tail — the shape that reads
        # as a highlight rather than a gradient.
        level = (1.0 - distance / width) ** 2
        out.append(char, style=_blend(base, peak, level))
    return out


def tool_group_title(
    count: int,
    running: str | None,
    tick: int,
    pal: FooterPalette,
) -> Text:
    """The collapsible tool-group header, animated while a tool is live.

    Args:
        count: How many tool calls the group holds.
        running: The tool currently executing, or None when the group is idle.
        tick: Status-tick counter driving the motion.
        pal: Active theme palette.

    Returns:
        The title as a styled ``Text``.
    """
    out = Text()
    out.append(f"{count} tool call" + ("" if count == 1 else "s"), style=f"bold {pal.text}")
    if running:
        out.append("   ")
        out.append(f"{spinner(tick)} ", style=f"bold {pal.accent}")
        out.append("running ", style=pal.dim)
        # The trailing ellipsis belongs to the label, not the animation, so it
        # sweeps with the rest of the words and the width stays constant.
        out.append_text(shimmer(f"{running}\u2026", tick, pal))
    return out
