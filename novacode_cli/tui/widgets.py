"""Widgets for the Nova TUI.

Extracted verbatim from :mod:`novacode_cli.tui.app` (which re-exports these
names for backward compatibility). This module must not import from app.py.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.strip import Strip
from textual.selection import Selection
from textual.theme import Theme
from textual.widget import Widget
from textual.message import Message
from textual.widgets import Input, Static, TextArea

from novacode_cli.image_utils import ImageData
from novacode_cli.input_utils import (
    PASTE_MIN_CHARS,
    PASTE_MIN_NEWLINES,
    PasteTracker,
    format_paste_placeholder,
)


# ---------------------------------------------------------------------------
# Matrix rain — animated home screen banner
# ---------------------------------------------------------------------------


class MatrixRain(Static):
    """A matrix-style digital rain with the NOVA ASCII logo composited on top.

    Columns of falling half-width katakana/hex characters cascade down with a
    fading trail (bright head, dimmer green tail). The ASCII banner is stamped
    over the rain each frame: its solid cells occlude the rain (logo in front),
    while regular-space gaps stay transparent so the rain shows *through* the
    art — "rain behind the banner". The logo is tinted with the active TUI
    theme's primary color, so it recolors live when the theme changes.

    Half-width katakana (U+FF66–FF9D) are single-cell, so they align exactly
    with the width-1 ASCII art — full-width katakana would drift the columns.
    """

    KATAKANA = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜｦﾝｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅ"

    def __init__(self, art: str = "", width: int | None = None) -> None:
        super().__init__("", id="matrix-rain")
        self._columns: list[dict] = []
        self._chars = list(MatrixRain.KATAKANA)
        self._width: int | None = None
        self._timer: Any = None  # set_interval handle for pause/resume
        self._needs_layout = True  # one layout pass after mount/reflow
        # Theme-derived render values, recomputed only when the theme changes
        # (keyed by _theme_key) instead of every frame.
        self._palette: tuple[str, str, str, str] | None = None
        self._art_style_str: str = ""
        self._palette_key: str | None = None
        # Rich Style objects for the palette, built once per theme. Segments
        # take a Style, and the buffers hold these objects directly so a frame
        # never parses a color string.
        self._styles: tuple[Style, Style, Style, Style] | None = None
        self._art_style_obj: Style | None = None
        # The rendered frame, as one Strip per row. render_line() serves from
        # here; _tick() replaces it.
        self._strips: list[Strip] = []
        self._configure(art, width)

    def _configure(self, art: str, width: int | None) -> None:
        """(Re)compute the grid + logo placement for the given art and width."""
        # Trim blank top/bottom lines so the logo sits tightly in the rain.
        art_lines = art.splitlines() if art else []
        while art_lines and not art_lines[0].strip():
            art_lines.pop(0)
        while art_lines and not art_lines[-1].strip():
            art_lines.pop()
        self._art_lines = art_lines

        art_w = max((cell_len(ln) for ln in art_lines), default=0)
        art_h = len(art_lines)
        # Fill the terminal width and centre the lockup within it. Subtract only
        # the transcript's horizontal padding (2 cells each side, see
        # `#transcript { padding: 1 2 }`); the scrollbar no longer reserves a
        # column (it is hidden and the gutter is `auto`), so the art is centred
        # across the full content width. Cap very wide terminals so the
        # per-frame build stays cheap.
        #
        # `art_w` stays in the max() as a floor: the art must never be wider
        # than the grid, or the composite would clip its right edge.
        usable = (width or 80) - 4
        self._col_count = min(max(usable, art_w, 60), 200)
        self._row_count = max(art_h + 6, 18)
        self._art_left = max(0, (self._col_count - art_w) // 2)
        self._art_top = 2  # a couple rows of rain above the lockup
        self._art_w = art_w  # the art's bounding box, measured with cell_len
        self._width = width

    def reflow(self, art: str, width: int | None) -> None:
        """Re-grid for a new terminal width (called on resize). No-op if same."""
        if width == self._width:
            return
        self._configure(art, width)
        if self.is_mounted:
            self._init_columns()  # rebuild rain columns for the new width
            self._apply_size()  # the grid changed, so the widget box must too
            self._needs_layout = True  # next frame must re-layout (size changed)

    def _theme_base_color(self):
        """The active theme's primary color as a Textual ``Color`` object.

        Handles ANSI theme names (``ansi_blue`` → ``blue``). A theme colour that
        cannot be read or parsed falls back to :data:`brand.FALLBACK_ACCENT` —
        the same accent the pre-TUI boot screen uses — rather than the old
        hardcoded green, which was a third brand colour disagreeing with both
        the red boot splash and the blue TUI.
        """
        from textual.color import Color

        from novacode_cli.brand import FALLBACK_ACCENT

        raw = None
        try:
            raw = self.app.current_theme.primary
        except Exception:  # noqa: BLE001
            try:
                raw = self.app.theme_variables.get("primary")
            except Exception:  # noqa: BLE001
                raw = None
        raw = (raw or FALLBACK_ACCENT).strip()
        if raw.startswith("ansi_"):
            raw = raw[len("ansi_") :]
        try:
            return Color.parse(raw)
        except Exception:  # noqa: BLE001
            return Color.parse(FALLBACK_ACCENT)

    def _art_style(self) -> str:
        """Bold style for the logo — the theme's primary color at full strength."""
        return f"bold {self._theme_base_color().hex}"

    def _theme_key(self) -> str:
        """The active theme's primary color string — the palette cache key."""
        from novacode_cli.brand import FALLBACK_ACCENT

        raw = None
        try:
            raw = self.app.current_theme.primary
        except Exception:  # noqa: BLE001
            try:
                raw = self.app.theme_variables.get("primary")
            except Exception:  # noqa: BLE001
                raw = None
        return (raw or FALLBACK_ACCENT).strip()

    def _ensure_theme_cache(self) -> None:
        """Recompute the palette + art style only when the theme color changes."""
        key = self._theme_key()
        if key != self._palette_key:
            self._palette = self._rain_palette()
            self._art_style_str = self._art_style()
            # Parsed once per theme, then stored in the frame buffers directly.
            # These are the identity-comparable singletons the run-length scan
            # in _build_strips relies on.
            self._styles = tuple(Style.parse(c) for c in self._palette)
            self._art_style_obj = Style.parse(self._art_style_str)
            self._palette_key = key

    def _rain_palette(self) -> tuple[str, str, str, str]:
        """(head, near, mid, tail) hex colors — a *dimmed* theme-tinted gradient.

        Derived from the theme color and darkened progressively so the rain reads
        as a subtle backdrop (no bright white head) and recolors with the theme.
        """
        base = self._theme_base_color()
        return (
            base.darken(0.15).hex,  # head — muted, not white
            base.darken(0.40).hex,  # near head
            base.darken(0.60).hex,  # mid trail
            base.darken(0.78).hex,  # tail — barely there
        )

    def _apply_size(self) -> None:
        """Pin the widget to the grid it renders.

        ``render_line`` produces no renderable for Textual to measure, so the
        auto-sizing a ``Static`` used to get from its content is gone and the
        box would collapse to zero height. The grid dimensions are the size.
        """
        self.styles.width = self._col_count
        self.styles.height = self._row_count

    def on_mount(self) -> None:
        self._init_columns()
        self._apply_size()
        self._strips = self._build_strips()  # paint something on the first frame
        self._needs_layout = True  # first frame must establish the widget size
        # ~15 fps: column speeds are scaled so the fall rate looks the same as
        # the old 25 fps, but each second costs 40% fewer frame builds and —
        # more importantly — 40% fewer Textual repaints on the main thread.
        self._timer = self.set_interval(0.066, self._tick)

    def pause(self) -> None:
        """Pause the rain timer (called when the app loses OS focus or rain is
        scrolled out of view)."""
        if self._timer is not None:
            self._timer.pause()

    def resume(self) -> None:
        """Resume the rain timer (called when the app regains OS focus)."""
        if self._timer is not None:
            self._timer.resume()

    def _init_columns(self) -> None:
        """Seed a column per grid column, already in flight.

        Heads are spread across the whole grid (and just above it) rather than
        stacked above row 0. The old seeding used ``uniform(-span, 0)``, which
        put every head off-screen, so the banner opened empty and the rain only
        filled in over the next ~10-20 seconds -- the "rain does not cover the
        window" complaint. Recycling a column still restarts it above the top
        (see ``_fill_buffers``), which is what keeps the fall continuous.
        """
        self._columns = []
        for _ in range(self._col_count):
            trail = random.randint(5, 14)
            self._columns.append(
                {
                    "pos": random.uniform(-trail, self._row_count),
                    "speed": random.uniform(0.07, 0.18),  # adjusted for 15fps
                    "trail": trail,
                }
            )
        # Pre-allocate frame buffers (reused per frame to avoid GC churn).
        # The style buffer holds Rich ``Style`` objects (None = blank cell), not
        # color strings: they go straight into Segments, so a frame does no
        # style parsing at all.
        self._frame_lines = [[" "] * self._col_count for _ in range(self._row_count)]
        self._frame_styles = [[None] * self._col_count for _ in range(self._row_count)]
        # Prebuilt blank rows copied in (slice-assign) to clear buffers at C speed.
        self._blank_line = [" "] * self._col_count
        self._blank_style = [None] * self._col_count
        # Precomputed random-char pool: strided per frame from a random offset so
        # the look stays random without a random.choice() call per cell.
        self._char_pool = [random.choice(self._chars) for _ in range(512)]

    def _tick(self) -> None:
        """Advance one frame of the rain, then stamp the logo over it.

        Early-returns (no work) when the widget is scrolled out of the visible
        viewport, so transcript messages push the rain off-screen cheaply.
        """
        # Pause animation updates while TTS is playing to prevent audio stuttering
        # due to thread / GIL contention on the main loop.
        try:
            vp = getattr(self.app, "_voice_pipeline", None)
            if vp is not None and vp.tts_active:
                return
        except Exception:  # noqa: BLE001
            pass

        # Skip the (expensive) frame build when the banner is scrolled out of the
        # transcript viewport. Both regions are in SCREEN coordinates, so they're
        # directly comparable: if the widget sits entirely above or below the
        # parent scroll-container's visible area, there's nothing to draw.
        import sys

        is_testing = "pytest" in sys.modules or getattr(self.app, "_driving", False)
        try:
            if not is_testing:
                p = self.parent
                visible = getattr(p, "region", None)
                if visible is not None:
                    r = self.region
                    if r.height == 0 or r.bottom <= visible.y or r.y >= visible.bottom:
                        return
        except Exception:  # noqa: BLE001
            pass

        # layout=False: the frame's row/col count is constant between reflows,
        # so a repaint suffices. The default layout=True re-laid-out the WHOLE
        # screen DOM (up to ~400 transcript widgets) on every frame — the
        # single biggest source of TUI jank while the rain was visible. A
        # one-shot layout pass still runs after mount/reflow (size changes).
        needs_layout = self._needs_layout
        self._needs_layout = False
        self._strips = self._build_strips()
        self.refresh(layout=needs_layout)

    def render_line(self, y: int) -> Strip:
        """Serve row *y* from the frame built by :meth:`_tick`.

        Rendering through ``render_line`` rather than ``Static.update()`` is the
        whole performance story of this widget. Handing Textual a Rich ``Text``
        of ~1000 style spans cost **14.7 ms per frame** on a 194-column grid —
        four times the cost of simulating the rain itself — so at 15 fps the
        banner burned 220 ms of every second on the main thread. That is what
        made typing feel laggy. Building Segments straight into Strips costs
        **1.6 ms** (9x), and measured event-loop p99 went 66 ms -> 16 ms, which
        is the same as with no rain at all.

        Note the fix is NOT "avoid re-parsing style strings" — that was the
        obvious guess and it is wrong. Passing pre-parsed ``Style`` objects into
        a ``Text`` measured *worse* (28 ms); the cost is ``Text``'s own span
        resolution and line division, so the only real fix is to not build one.
        """
        if 0 <= y < len(self._strips):
            return self._strips[y]
        return Strip.blank(self._col_count)

    def _build_strips(self) -> list[Strip]:
        """Advance one frame and render it as one ``Strip`` per row."""
        self._fill_buffers()
        cols = self._col_count
        lines = self._frame_lines
        styles = self._frame_styles
        strips: list[Strip] = []
        for y in range(self._row_count):
            line = lines[y]
            st = styles[y]
            segments: list[Segment] = []
            x = 0
            while x < cols:
                s = st[x]
                j = x + 1
                # Identity, not equality: every style in the buffer is one of
                # the cached per-theme singletons (or None), so `is` is both
                # correct here and cheaper than Style.__eq__ per cell.
                while j < cols and st[j] is s:
                    j += 1
                segments.append(
                    Segment("".join(line[x:j]) if s else " " * (j - x), s)
                )
                x = j
            strips.append(Strip(segments, cols))
        return strips

    def render(self) -> Text:
        """The current frame as text.

        Textual never calls this — :meth:`render_line` is overridden, which
        takes precedence — but ``Static.render`` would otherwise return the
        empty renderable this widget no longer sets, which reads as "the banner
        is blank" to anything that inspects it. Returning the live frame keeps
        that honest. It does NOT advance the rain.
        """
        return self._frame_text()

    def _build_frame(self) -> Text:
        """Advance one frame and return it as a Rich ``Text``.

        NOT the render path — :meth:`render_line` is. Kept because plain text is
        the readable form to assert against (grid width, which glyphs may
        appear), and those checks are worth keeping.
        """
        self._fill_buffers()
        return self._frame_text()

    def _frame_text(self) -> Text:
        """Render the buffers as text, without advancing the simulation."""
        cols = self._col_count
        text = Text()
        for y in range(self._row_count):
            line = self._frame_lines[y]
            st = self._frame_styles[y]
            x = 0
            while x < cols:
                s = st[x]
                j = x + 1
                while j < cols and st[j] is s:
                    j += 1
                text.append("".join(line[x:j]) if s else " " * (j - x), s)
                x = j
            if y < self._row_count - 1:
                text.append("\n")
        return text

    def _fill_buffers(self) -> None:
        """Advance the simulation one frame into the reusable cell buffers."""
        cols = self._col_count
        rows = self._row_count
        lines = self._frame_lines
        styles = self._frame_styles

        # Clear buffers for the new frame (C-level slice copy of prebuilt rows).
        blank_l = self._blank_line
        blank_s = self._blank_style
        for y in range(rows):
            lines[y][:] = blank_l
            styles[y][:] = blank_s

        self._ensure_theme_cache()
        head_c, near_c, mid_c, tail_c = self._styles
        pool = self._char_pool
        pool_len = len(pool)
        idx = random.randrange(pool_len)  # one RNG call per frame

        # The rain is drawn on every column, including the ones the art covers.
        # Confining it was tried and removed: the art is stamped *after* the
        # rain, so it overwrites every inked cell anyway -- masking those cells
        # changed nothing observable (measured 0 katakana visible on inked cells
        # with the confinement removed). Skipping the art's *bounding box* was
        # worse than useless: it is ~74 of the 76 columns an 80-column terminal
        # draws, so skipping it on every row carved a full-height dead stripe and
        # the rain looked like it stopped at the portrait.
        for col, d in enumerate(self._columns):
            d["pos"] += d["speed"]
            if d["pos"] > rows + d["trail"]:  # reset when fully off-screen
                d["pos"] = random.uniform(-rows, -3)
                d["speed"] = random.uniform(0.08, 0.23)  # adjusted for 15fps
                d["trail"] = random.randint(5, 14)

            tail_start = max(0, int(d["pos"]) - d["trail"])
            head = min(rows - 1, int(d["pos"]))
            for y in range(tail_start, head + 1):
                dist = head - y
                lines[y][col] = pool[idx]
                idx += 1
                if idx == pool_len:
                    idx = 0
                if dist == 0:
                    styles[y][col] = head_c
                elif dist <= 2:
                    styles[y][col] = near_c
                elif dist <= 5:
                    styles[y][col] = mid_c
                else:
                    styles[y][col] = tail_c

        # Composite the logo on top. Solid art cells occlude the rain; regular
        # spaces stay transparent so the rain shows through the gaps.
        if self._art_lines:
            art_style = self._art_style_obj
            for ay, art_line in enumerate(self._art_lines):
                gy = self._art_top + ay
                if not 0 <= gy < rows:
                    continue
                row_l = lines[gy]
                row_s = styles[gy]
                left = self._art_left
                # Advance by CELL, not by codepoint. Rich gives a zero-width
                # variation selector (U+FE0E/U+FE0F) no cell of its own, so
                # indexing a per-codepoint loop by that number would shift every
                # cell after it one column right. brand.py keeps such pairs out
                # of the art; this makes the compositor agree with cell_len
                # regardless.
                ax = 0
                for ch in art_line:
                    if ch in ("\ufe0e", "\ufe0f"):
                        continue  # zero-width: shares the previous char's cell
                    if ch != " ":
                        gx = left + ax
                        if 0 <= gx < cols:
                            row_l[gx] = ch
                            row_s[gx] = art_style
                    ax += cell_len(ch)



# Nova's default palette, registered as a real Textual theme so /theme can swap
# it for any other theme. Previously these colors were hardcoded as CSS
# variables ($primary: …) at the top of NovaApp.CSS, which *overrode* the active
# theme and made theme switching a no-op. Defining them here instead lets the
# active theme drive every $variable, so /theme actually changes the UI.
NOVA_TOKYO_NIGHT = Theme(
    name="tokyo-night",
    primary="#7aa2f7",
    secondary="#9ece6a",
    accent="#bb9af7",
    success="#73daca",
    warning="#e0af68",
    error="#f7768e",
    surface="#1a1b26",
    panel="#24283b",
    background="#13141d",
    foreground="#c0caf5",
    boost="#2f3346",
    dark=True,
    variables={
        "text-muted": "#565f89",
        "border": "#3b4261",
    },
)
# The Matrix theme: phosphor green on near-black, the palette of the film's
# terminal. Registered alongside tokyo-night so /theme can select it. Kept
# deliberately monochrome-green — the accent/secondary/success hues are all
# shades of the same phosphor so the UI reads as one CRT, with `error` the only
# non-green (a red alert must still stand out on a green screen).
NOVA_MATRIX = Theme(
    name="matrix",
    primary="#00ff41",  # the classic Matrix green
    secondary="#00b32d",  # dimmer phosphor
    accent="#39ff14",  # neon green, for highlights
    success="#00ff41",
    warning="#c8ff00",  # acid yellow-green
    error="#ff3b30",  # the one non-green: alerts must not blend in
    surface="#0a0f0a",
    panel="#0d140d",
    background="#000600",
    foreground="#b6ffb6",
    boost="#12200f",
    dark=True,
    variables={
        "text-muted": "#3f7a3f",
        "border": "#1f4d1f",
    },
)
DEFAULT_THEME = "tokyo-night"


class SessionHeader(Horizontal):
    """A structured header showing session metadata as pills.

    Pills include: Model, Sandbox, Memory, and CWD breadcrumbs.
    """

    DEFAULT_CSS = """
    SessionHeader {
        classes: "session-header";
    }
    """

    def __init__(self, model: str, sandbox: str, cwd: str, memory: str) -> None:
        super().__init__()
        self.model = model
        self.sandbox = sandbox
        self.cwd = cwd
        self.memory = memory

    def compose(self) -> ComposeResult:
        yield Static(f"🤖 {self.model}", classes="session-pill pill-model")
        yield Static(f"📦 {self.sandbox}", classes="session-pill pill-sandbox")
        yield Static(f"🧠 {self.memory}", classes="session-pill pill-memory")
        yield Static(f"📁 {self.cwd}", classes="breadcrumb")

    def update_info(self, model: str, sandbox: str, cwd: str, memory: str) -> None:
        self.model = model
        self.sandbox = sandbox
        self.cwd = cwd
        self.memory = memory
        self.refresh()


class NovaStatusBar:
    """Legacy dummy class preserved for tests compatibility."""

    pass


class SelectableStatic(Static):
    """A ``Static`` whose Rich-rendered content can be text-selected.

    Textual's default ``Widget.get_selection`` reads ``self._render()`` and only
    handles a ``Text``/``Content`` visual. A ``Static`` holding a Rich renderable
    (``Markdown``, ``Table``, ``Group``, …) renders to a ``RichVisual`` instead,
    so ``get_selection`` returns ``None`` and drag-selecting the widget yields
    nothing — the text is visible but not selectable.

    This subclass falls back to the *rendered* text (the strips the compositor
    actually paints) so selection works for any Rich renderable, and the offsets
    line up with what the user sees. Plain ``Text``/``Content`` bodies keep the
    base implementation, which is already correct and cheaper.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract the selected text, falling back to the rendered strips.

        Args:
            selection: The selection range, in cell offsets.

        Returns:
            A ``(text, ending)`` tuple, or ``None`` if nothing could be extracted.
        """
        visual = self._render()
        # Text/Content visuals are handled correctly by the base class.
        from textual.content import Content

        if isinstance(visual, (Text, Content)):
            return super().get_selection(selection)

        # Rich renderable → RichVisual. Re-render it to strips and join the
        # visible line text, which is what the selection offsets index into.
        try:
            from textual.visual import RichVisual, Visual

            if not isinstance(visual, RichVisual):
                return super().get_selection(selection)
            width = max(1, self.size.width or self.content_size.width or 1)
            strips = Visual.to_strips(self, visual, width, None, self.visual_style)
            # rstrip each line: the compositor pads every strip to the widget
            # width, so a copied selection would otherwise carry a screenful of
            # trailing spaces per line.
            text = "\n".join(strip.text.rstrip() for strip in strips)
        except Exception:  # noqa: BLE001 — selection must never break rendering
            return super().get_selection(selection)
        return selection.extract(text), "\n"


class TranscriptScroll(VerticalScroll):
    """A scroll region that announces when it is (or stops being) at the bottom.

    Textual posts no message when a widget scrolls — the only hook is the
    ``scroll_y`` reactive watcher — so the "jump to latest" affordance had no
    way to know the user had scrolled up. This subclass posts
    :class:`TranscriptScroll.AtEndChanged` whenever the at-the-bottom state
    flips, which is exactly when the button must appear or hide.
    """

    class AtEndChanged(Message):
        """Posted when the scroll region reaches or leaves the bottom."""

        def __init__(self, scroll: TranscriptScroll, *, at_end: bool) -> None:
            """Record the scroll region and its new at-the-bottom state."""
            self.scroll = scroll
            self.at_end = at_end
            super().__init__()

    class Scrolled(Message):
        """Posted on every scroll position change (for the distance readout)."""

        def __init__(
            self, scroll: TranscriptScroll, *, old_y: float, new_y: float
        ) -> None:
            """Record the scroll region that moved and its before/after offset.

            The delta is carried on the message rather than compared against a
            global "last position" in the app: with several session panes, a
            global would be stale after a pane switch and misread a downward
            scroll as an upward one.
            """
            self.scroll = scroll
            self.old_y = old_y
            self.new_y = new_y
            super().__init__()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the scroll region and seed the at-the-bottom state."""
        super().__init__(*args, **kwargs)
        self._at_end: bool | None = None

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Announce scroll changes so the jump-to-latest affordance can react."""
        super().watch_scroll_y(old_value, new_value)
        # Post on every change so the "N lines below" readout stays current;
        # AtEndChanged only on a flip, so the show/hide work is not repeated.
        self.post_message(self.Scrolled(self, old_y=old_value, new_y=new_value))
        at_end = self.is_vertical_scroll_end or (self.max_scroll_y - self.scroll_y) <= 1
        if at_end != self._at_end:
            self._at_end = at_end
            self.post_message(self.AtEndChanged(self, at_end=at_end))


class ChatMessage(Vertical):
    """A single transcript entry: a left accent bar, a role header, and a body.

    role_class selects the accent color (user / nova / reason). The body is set
    via :meth:`update_body` with any rich renderable (plain Text while streaming,
    Markdown once committed)."""

    DEFAULT_CSS = """
    /* Even, consistent cards: a colored left accent bar + padding. Only the
       accent color and header differ between roles (minimal accent-bar style).
       The base background matches the app's own, so the agent's replies read as
       plain transcript rather than a raised card. */
    ChatMessage {
        height: auto;
        margin: 1 0;
        padding: 1 2;
        background: $background;
    }
    /* Hover cue that a message is clickable (click = copy it). User turns only:
       the agent's replies are plain reading surface and must not shift color
       under the cursor. */
    ChatMessage.user:hover { background: $boost; }
    ChatMessage > .role { text-style: bold; }
    ChatMessage > .body { height: auto; }
    ChatMessage.user { border-left: thick $primary; }
    ChatMessage.reason { border-left: thick $panel; color: $text-muted; }
    /* Collapsed reasoning: only the header row remains, so a long thinking
       trace stays in the transcript as a one-line affordance. */
    ChatMessage.collapsed > .body { display: none; }
    ChatMessage.collapsed { height: auto; }
    /* User turns stand out from the agent's: a raised panel behind the text and
       full-brightness foreground, so what YOU typed is easy to find when
       scrolling back through a long transcript. The accent bar stays $primary. */
    ChatMessage.user { background: $panel; color: $foreground; }
    ChatMessage.user > .role { color: $primary; }
    """

    def __init__(
        self, header: Text, role_class: str, *, collapsible: bool = False
    ) -> None:
        super().__init__(classes=role_class)
        self._header = header
        # Plain-text form of the body, kept in sync by update_body so the message
        # can be copied verbatim (click-to-copy / the /copy command) without
        # re-deriving it from the rendered markdown.
        self.raw_text: str = ""
        # Body renderable received before compose() finished mounting the `.body`
        # child. Applied in on_mount so a fast first stream chunk isn't lost.
        self._pending_body: Any = None
        # Collapsible messages (reasoning traces) keep their body but can be
        # folded to a one-line header, so a long thinking trace stays in the
        # transcript without dominating it.
        self._collapsible = collapsible
        self._collapsed = False
        self.tooltip = "Click to copy this message"

        # Parse and store custom border color if specified
        self._custom_color = None
        if header.style:
            from rich.style import Style

            if isinstance(header.style, Style) and header.style.color:
                self._custom_color = header.style.color.name
            else:
                style_str = str(header.style)
                import re

                m = re.search(r"#(?:[0-9a-fA-F]{3}){1,2}\b", style_str)
                if m:
                    self._custom_color = m.group(0)
                elif "green" in style_str:
                    self._custom_color = "#10b981"

    def compose(self) -> ComposeResult:
        yield Static(self._header, classes="role")
        if isinstance(self._pending_body, Widget):
            self._pending_body.add_class("body")
            yield self._pending_body
        else:
            yield SelectableStatic(self._pending_body or "", classes="body")

    def on_mount(self) -> None:
        # Apply custom border color if set. Only user turns carry an accent bar
        # (see DEFAULT_CSS) — the agent's replies are unbarred, so a header color
        # must not re-introduce one.
        if self._custom_color and self.has_class("user"):
            self.styles.border_left = ("thick", self._custom_color)

        # If update_body ran before the `.body` child existed, apply the stashed
        # renderable now that the children are mounted.
        if self._pending_body is not None:
            if not isinstance(self._pending_body, Widget):
                try:
                    self.query_one(".body", Static).update(self._pending_body)
                except NoMatches:
                    pass
            self._pending_body = None

    def set_collapsed(self, *, collapsed: bool) -> None:
        """Fold/unfold a collapsible message's body (no-op for normal messages).

        Args:
            collapsed: True to hide the body, False to show it.
        """
        if not self._collapsible:
            return
        self._collapsed = collapsed
        self.set_class(collapsed, "collapsed")
        self._refresh_header_affordance()

    def toggle_collapsed(self) -> None:
        """Flip the collapsed state of a collapsible message."""
        self.set_collapsed(collapsed=not self._collapsed)

    def _refresh_header_affordance(self) -> None:
        """Append a ▸/▾ caret to the header so the fold state is visible."""
        if not self._collapsible:
            return
        caret = "▸" if self._collapsed else "▾"
        header = self._header.copy()
        header.append(f"  {caret}", style="dim")
        try:
            self.query_one(".role", Static).update(header)
        except Exception:  # noqa: BLE001 — header refresh must never break render
            pass

    def update_header(self, header: Text) -> None:
        self._header = header
        try:
            role_static = self.query_one(".role", Static)
            role_static.update(header)
        except Exception:
            pass

        # Parse and store custom border color if specified
        self._custom_color = None
        if header.style:
            from rich.style import Style

            if isinstance(header.style, Style) and header.style.color:
                self._custom_color = header.style.color.name
            else:
                style_str = str(header.style)
                import re

                m = re.search(r"#(?:[0-9a-fA-F]{3}){1,2}\b", style_str)
                if m:
                    self._custom_color = m.group(0)
                elif "green" in style_str:
                    self._custom_color = "#10b981"

        if self._custom_color and self.has_class("user"):
            self.styles.border_left = ("thick", self._custom_color)

    def update_body(self, renderable: Any) -> None:
        self.raw_text = self._renderable_text(renderable)
        try:
            body = self.query_one(".body")
        except NoMatches:
            # Children not mounted yet (Textual composes asynchronously). Stash
            # the renderable; on_mount/compose will apply it. Prevents the
            # "No nodes match '.body'" crash on a fast first stream chunk.
            self._pending_body = renderable
            return

        if isinstance(renderable, Widget):
            if body is not renderable:
                body.remove()
                renderable.add_class("body")
                self.mount(renderable)
        else:
            if isinstance(body, SelectableStatic):
                body.update(renderable)
            else:
                body.remove()
                new_body = SelectableStatic(renderable, classes="body")
                self.mount(new_body)

    @staticmethod
    def _renderable_text(renderable: Any) -> str:
        """Best-effort plain text for a body renderable (Markdown/Text/other)."""
        if isinstance(renderable, Markdown):
            return renderable.markup
        if isinstance(renderable, Text):
            return renderable.plain
        if hasattr(renderable, "initial_cmd"):
            return f"$ {renderable.initial_cmd}"
        return str(renderable)

    def on_click(self, event: events.Click) -> None:
        """Click a message to copy its full text to the clipboard.

        This is deterministic and terminal-independent — unlike mouse
        drag-selection, which a captured-mouse TUI can't reliably support.
        Drag-selection still works: Textual only fires Click when there was no
        drag, and an explicit selection takes precedence here.
        """
        body = (self.raw_text or "").strip()
        if not body:
            return
        app = self.app
        try:
            if app.screen.get_selected_text():
                return  # honor an explicit selection — ctrl+c will copy it
        except Exception:  # noqa: BLE001
            pass
        try:
            app.copy_to_clipboard(body)
            app._log(  # type: ignore[attr-defined]
                Text(f"📋 Copied message ({len(body):,} chars)", style="dim")
            )
            event.stop()
        except Exception:  # noqa: BLE001
            pass



class PromptInput(TextArea):
    """Main prompt input that collapses large pastes into a compact placeholder.

    Textual's single-line ``Input`` keeps only the *first line* of a paste, so a
    multi-line paste would silently lose everything after the first newline.
    Instead, a large paste (>= ``PASTE_MIN_CHARS`` chars or ``PASTE_MIN_NEWLINES``
    newlines) is stored in the app's :class:`PasteTracker` and shown inline as
    ``[paste #N +M lines]``; the placeholder is resolved back to the full text on
    submit (see :meth:`NovaApp.on_input_submitted`). Small pastes fall through to
    Textual's default behaviour.

    Reuses the same tracker/threshold/format helpers as the legacy prompt_toolkit
    input (:mod:`novacode_cli.input`) so both UIs behave identically.
    """

    # After the last Paste fragment, how long an in-progress paste stays "open"
    # for more fragments to merge into it. A single large paste is often split
    # by the terminal into several Paste events that can arrive hundreds of ms
    # apart, so this is generous — it does NOT block typing (that's the separate
    # key-drop window below) and is also ended early by any real keystroke.
    PASTE_MERGE_WINDOW = 1.5
    # How long stray per-character key events are dropped after a Paste fragment
    # (Windows terminals echo a paste as queued keystrokes). Short, so real
    # typing right after a paste is never swallowed.
    PASTE_KEYDROP_WINDOW = 0.05

    def __init__(self, *args, **kwargs) -> None:
        self._paste_tracker: PasteTracker | None = kwargs.pop("paste_tracker", None)
        self._on_large_paste = kwargs.pop("on_large_paste", None)
        # Called with the new ImageData when Ctrl+V finds an image on the
        # clipboard; returns the id to insert as a placeholder, or "" to
        # decline. Left unset, Ctrl+V is a plain text paste.
        self._on_clipboard_image = kwargs.pop("on_clipboard_image", None)
        super().__init__(*args, **kwargs)
        # On Windows terminals, pasting fires BOTH a Paste event AND individual
        # key events for each character.  We stop the Paste event, but the
        # keystrokes are already queued.  This flag drops them until the key-drop
        # window expires.
        self._paste_active = False
        self._keydrop_timer: Any = None
        # Identity-merge state for coalescing a fragmented paste: while a paste
        # is "open", later fragments are appended to this SAME paste id (one
        # block, one placeholder) instead of each becoming its own [paste #N].
        self._active_paste_id: str | None = None
        self._active_paste_ph: str = ""
        self._merge_timer: Any = None

    class Submitted(Message):
        """Posted when the user presses enter to send the prompt.

        ``TextArea`` has no Submitted message of its own (enter inserts a
        newline there), so the prompt defines one to keep the app-side
        contract identical to the old ``Input``-based widget.
        """

        def __init__(self, prompt_input: "PromptInput", value: str) -> None:
            self.input = prompt_input
            self.value = value
            super().__init__()

    @property
    def value(self) -> str:
        """Alias of ``text`` so callers written against ``Input`` still work."""
        return self.text

    @value.setter
    def value(self, new: str) -> None:
        self.text = new

    @property
    def cursor_position(self) -> int:
        """Cursor offset into ``text`` (``Input``-compatible).

        ``TextArea`` exposes a (row, column) ``cursor_location``; the palette
        and @-completion were written against a flat offset, so translate.
        """
        try:
            row, col = self.cursor_location
        except Exception:  # noqa: BLE001
            return len(self.text)
        lines = self.text.split("\n")[:row]
        return sum(len(line) + 1 for line in lines) + col

    @cursor_position.setter
    def cursor_position(self, offset: int) -> None:
        text = self.text
        offset = max(0, min(offset, len(text)))
        before = text[:offset]
        row = before.count("\n")
        col = offset - (before.rfind("\n") + 1)
        self.move_cursor((row, col))

    async def _on_key(self, event: events.Key) -> None:
        # enter sends; shift+enter (and ctrl+j, which some terminals send
        # instead) inserts a real newline. TextArea does the opposite by
        # default, and a chat prompt wants enter to mean "send".
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self._end_paste_merge()
            self.post_message(self.Submitted(self, self.text))
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self._end_paste_merge()
            self.insert("\n")
            return
        # ctrl+v: prefer a clipboard IMAGE, else fall back to a text paste.
        #
        # Intercepting here is necessary rather than optional: when this
        # ``_on_key`` does not stop the event, Textual never runs the ctrl+v
        # binding, so the built-in ``action_paste`` is unreachable either way.
        # Reading the clipboard is blocking, so it runs in a worker.
        if event.key == "ctrl+v":
            event.prevent_default()
            event.stop()
            self._end_paste_merge()
            self.run_worker(self._paste_clipboard(), group="clipboard")
            return
        # Drop ALL key events during the key-drop window. On Windows terminals,
        # pasting fires per-character keystrokes after the Paste event — the old
        # `len(event.key) == 1` filter was too narrow because some terminals send
        # the *entire pasted text* as a single key event (e.g. event.key == "use"),
        # which has len > 1 and was let through, duplicating the paste.
        if self._paste_active:
            return
        # A real keystroke means the paste burst is over — stop merging further
        # fragments into it so the next paste starts fresh.
        self._end_paste_merge()
        await super()._on_key(event)

    async def _paste_clipboard(self) -> None:
        """Paste a clipboard image if there is one, else the clipboard text.

        The image read is platform-specific and blocking (PIL / a subprocess),
        so it happens on a worker thread; the widget stays responsive for the
        fraction of a second that takes.
        """
        if self._on_clipboard_image is not None:
            try:
                image = await asyncio.to_thread(self._read_clipboard_image)
            except Exception:  # noqa: BLE001 — never break input over this
                image = None
            if image is not None:
                try:
                    image_id = self._on_clipboard_image(image)
                except Exception:  # noqa: BLE001
                    image_id = ""
                if image_id:
                    # Trailing space so typing continues after the placeholder.
                    self.insert(f"[{image_id}] ")
                    return
        # No image (or the app declined it): plain text paste.
        self.action_paste()

    @staticmethod
    def _read_clipboard_image() -> ImageData | None:
        """Read an image off the system clipboard, or ``None``.

        Imported lazily so Pillow is only pulled in when a paste actually asks
        for it — it is not needed on the import path of the TUI.
        """
        from novacode_cli.image_utils import get_clipboard_image

        return get_clipboard_image()

    def _on_paste(self, event: events.Paste) -> None:
        text = event.text
        # Blank this out before Input._on_paste runs (Textual calls _on_* handlers
        # for every class in the MRO). Without this, the parent inserts the text a
        # second time, producing duplicates like "useuse".
        event.text = ""
        event.stop()

        if not text:
            # An EMPTY paste is how a terminal reports Ctrl+V on an image: the
            # terminal owns ctrl+v, so the key never reaches ``_on_key``, and
            # with no text for it to paste it delivers an empty bracketed paste
            # (verified on Windows Terminal). Treat it as "look at the clipboard
            # for an image" — the only signal available on this path.
            # Guarded on the callback so a plain empty paste stays a no-op.
            if self._on_clipboard_image is not None:
                self.run_worker(self._paste_clipboard(), group="clipboard")
            return
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        tracker = self._paste_tracker

        if self._active_paste_id is not None and tracker is not None:
            # Continuation of a fragmented paste: append to the SAME paste so the
            # whole thing stays one block for the agent, and refresh the inline
            # placeholder's line count in place (same id, updated count).
            old_ph = self._active_paste_ph
            tracker.extend_paste(self._active_paste_id, normalized)
            full = tracker.get_paste(self._active_paste_id) or normalized
            new_ph = format_paste_placeholder(self._active_paste_id, full)
            if new_ph != old_ph and old_ph in self.text:
                self.text = self.text.replace(old_ph, new_ph, 1)
                self.move_cursor(self.document.end)
            self._active_paste_ph = new_ph
        elif tracker is not None and (
            len(normalized) >= PASTE_MIN_CHARS or normalized.count("\n") >= PASTE_MIN_NEWLINES
        ):
            # First fragment of a large paste: create the placeholder and open
            # the merge window so any trailing fragments fold into this one.
            paste_id = tracker.add_paste(normalized)
            placeholder = format_paste_placeholder(paste_id, normalized)
            self.insert(placeholder + " ")
            self._active_paste_id = paste_id
            self._active_paste_ph = placeholder
        else:
            # Small paste — won't fragment; insert literally, no merge needed.
            self.insert(normalized)

        # Drop the echo keystrokes for a short moment after each fragment.
        self._paste_active = True
        if self._keydrop_timer is not None:
            self._keydrop_timer.stop()
        self._keydrop_timer = self.set_timer(
            self.PASTE_KEYDROP_WINDOW, lambda: setattr(self, "_paste_active", False)
        )
        # Keep the paste "open" for more fragments; close it after a quiet gap.
        if self._merge_timer is not None:
            self._merge_timer.stop()
        self._merge_timer = self.set_timer(self.PASTE_MERGE_WINDOW, self._end_paste_merge)

    def _end_paste_merge(self) -> None:
        """Close the current paste so the next paste/fragment starts fresh.

        Logs the collapsed-paste note to the transcript once, with the FINAL
        merged size, so a fragmented paste shows a single accurate notice.
        """
        if self._merge_timer is not None:
            self._merge_timer.stop()
            self._merge_timer = None
        if self._active_paste_id is None:
            return
        if self._on_large_paste is not None and self._paste_tracker is not None:
            full = self._paste_tracker.get_paste(self._active_paste_id)
            if full is not None:
                self._on_large_paste(self._active_paste_ph, len(full))
        self._active_paste_id = None
        self._active_paste_ph = ""


class TuiInitRenderer:
    """Adapter implementing ``InitRenderer`` for the Textual TUI path.

    The exploration prompt streams through the TUI's native chat.
    """

    def __init__(self, app: NovaApp) -> None:
        self._app = app

    def emit(self, event) -> None:
        """Pipeline progress event — no-op."""
        pass

    def result(self, result) -> None:
        """Log the final pipeline outcome."""
        if not result.ok and result.message:
            self._app._log(Text(result.message, style="yellow"))

    async def run_exploration(
        self,
        project_root: Path,
        nova_md_path: Path,
        agent,
        session_state,
        assistant_id: str,
        token_tracker,  # noqa: ARG002
    ) -> None:
        """Stream the exploration prompt through the TUI."""
        from novacode_cli.prompts import render_template

        prompt = render_template(
            "init_exploration.jinja",
            project_root=str(project_root),
            nova_md_path=str(nova_md_path),
        )
        prev = session_state.auto_approve
        session_state.auto_approve = True
        try:
            await self._app._stream_prompt(prompt)
        finally:
            session_state.auto_approve = prev


