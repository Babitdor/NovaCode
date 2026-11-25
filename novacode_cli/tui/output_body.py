"""Render bounded command output one visible row at a time."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich._wrap import divide_line
from rich.cells import cell_len
from rich.text import Text
from textual.strip import Strip
from textual.widget import Widget

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.highlighter import Highlighter
    from textual.geometry import Region, Size
    from textual.selection import Selection


class OutputBody(Widget):
    """Keep lightweight wrap offsets; materialize styled text only when visible.

    A Static containing the entire log measures and renders all its text, even
    though its parent shows only a few rows. Repeating that for each live write
    allocates full-output text, spans, and strips on the UI loop.
    """

    ALLOW_SELECT = True
    _MAX_CACHED_ROWS = 128

    def __init__(self, *, wrap: bool, highlighter: Highlighter | None) -> None:
        """Start with no logical lines or prepared rendering rows."""
        super().__init__(classes="output-text")
        self._wrap = wrap
        self._highlighter = highlighter
        self._lines: tuple[Text, ...] = ()
        self._rows: list[tuple[Text, int, int]] = []
        self._row_width = -1
        self._wrap_width = -1
        self._wrap_cache: dict[int, list[tuple[Text, int, int]]] = {}
        # Aligned with _rows once wrapped: which logical lines the rows came
        # from, and how many rows each contributed at the current width.
        self._row_line_ids: list[int] = []
        self._row_counts: list[int] = []
        self._last_layout_rows = 0

    @property
    def content(self) -> Text:
        """Materialize the complete output only for an explicit consumer."""
        return Text("\n").join(self._lines)

    def replace_lines(self, lines: Iterable[Text]) -> None:
        """Replace references to logical lines without joining or rendering them.

        Streaming appends (and head evictions) update the prepared rows
        incrementally — a full re-wrap of every retained line per 50 ms flush
        re-created tens of thousands of row tuples per panel on the UI loop.
        The refresh is a full layout only while the body's height can still
        change; once the visible height saturates, repaints are paint-only.
        """
        new = tuple(lines)
        if self._row_width > 0 and self._rows:
            old_ids = self._row_line_ids
            new_ids = [id(line) for line in new]
            added = None
            trim = 0
            if len(new) >= len(old_ids) and new_ids[: len(old_ids)] == old_ids:
                added = new[len(old_ids) :]
            else:
                # Streaming with head eviction: new == old[k:] + added.
                for k in range(1, len(old_ids) + 1):
                    tail = old_ids[k:]
                    if new_ids[: len(tail)] == tail:
                        trim = k
                        added = new[len(tail) :]
                        break
            if added is not None:
                if trim:
                    # Pop before trimming: old_ids aliases _row_line_ids, so
                    # deleting first would shift the ids under us.
                    for dropped_id in old_ids[:trim]:
                        self._wrap_cache.pop(dropped_id, None)
                    dropped = sum(self._row_counts[:trim])
                    del self._rows[:dropped]
                    del self._row_counts[:trim]
                    del self._row_line_ids[:trim]
                self._append_rows(added)
                self._lines = new
                self._refresh_after_replace()
                return
        self._lines = new
        retained = {id(line) for line in new}
        self._wrap_cache = {key: rows for key, rows in self._wrap_cache.items() if key in retained}
        self._rows.clear()
        self._row_line_ids.clear()
        self._row_counts.clear()
        self._row_width = -1
        # Rows were discarded: the next refresh must lay out regardless of the
        # row total matching the previous pass.
        self._last_layout_rows = -1
        self._refresh_after_replace()

    def _line_boundaries(self, plain: str, width: int) -> list[int]:
        """Row-end offsets for one logical line at *width*.

        ``rich._wrap.divide_line`` walks every word in Python, which made long
        build lines (thousands of words, appended 20x/s) the single hottest
        call in the TUI. ASCII lines — the overwhelming majority of command
        output — get an O(len/width) scan instead; the break semantics match
        (break after a space, hard-break overlong words). Non-ASCII lines keep
        rich's cell-aware divider.
        """
        if not self._wrap:
            return []
        if plain.isascii():
            boundaries: list[int] = []
            pos = 0
            n = len(plain)
            while n - pos > width:
                cut = plain.rfind(" ", pos, pos + width)
                if cut <= pos:
                    cut = pos + width
                else:
                    cut += 1  # keep the space at the end of the row
                boundaries.append(cut)
                pos = cut
            return boundaries
        return divide_line(plain, width, fold=True)

    def _append_rows(self, lines: tuple[Text, ...]) -> None:
        """Wrap only the new lines at the current width and extend the rows."""
        width = self._row_width
        for line in lines:
            cached = self._wrap_cache.get(id(line))
            if cached is None:
                boundaries = self._line_boundaries(line.plain, width)
                start = 0
                prepared = []
                for end in (*boundaries, len(line)):
                    prepared.append((line, start, end))
                    start = end
                self._wrap_cache[id(line)] = prepared
                cached = prepared
            self._rows.extend(cached)
            self._row_counts.append(len(cached))
            self._row_line_ids.append(id(line))

    def _wrap_rows(self, width: int) -> None:
        """Rebuild every row for *width* (initial load, resize, invalidation)."""
        width = max(1, width)
        if self._row_width == width and len(self._row_line_ids) == len(self._lines):
            return
        if self._wrap_width != width:
            self._wrap_cache.clear()
            self._wrap_width = width
        rows = []
        counts: list[int] = []
        ids: list[int] = []
        for line in self._lines:
            cached = self._wrap_cache.get(id(line))
            if cached is None:
                boundaries = self._line_boundaries(line.plain, width)
                start = 0
                prepared = []
                for end in (*boundaries, len(line)):
                    prepared.append((line, start, end))
                    start = end
                self._wrap_cache[id(line)] = prepared
                cached = prepared
            rows.extend(cached)
            counts.append(len(cached))
            ids.append(id(line))
        self._rows = rows
        self._row_counts = counts
        self._row_line_ids = ids
        self._row_width = width

    def _refresh_after_replace(self) -> None:
        """Lay out when the row total changed; repaint when it did not.

        The expensive part of a flush was re-wrapping every retained line, and
        that is incremental now — the layout pass itself is cheap. A saturated
        log that evicts one line while appending another keeps its row total,
        so its steady-state flushes are paint-only.
        """
        rows = len(self._rows)
        self.refresh(layout=rows != self._last_layout_rows)
        self._last_layout_rows = rows

    def get_content_width(self, container: Size, viewport: Size) -> int:  # noqa: ARG002
        """Use available width when wrapping, otherwise measure logical lines."""
        if self._wrap:
            return container.width
        return max((cell_len(line.plain) for line in self._lines), default=0)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:  # noqa: ARG002
        """Measure row count using wrap offsets, without creating styled strips."""
        self._wrap_rows(width)
        return len(self._rows)

    def render_line(self, y: int) -> Strip:
        """Render one visible row with its original styles and selection offsets."""
        self._wrap_rows(self.size.width)
        width = self.size.width
        if not 0 <= y < len(self._rows):
            return Strip.blank(width, self.rich_style)
        source, start, end = self._rows[y]
        row = source[start:end]
        row.rstrip_end(width)
        row.truncate(width, overflow="crop")
        if self._highlighter is not None:
            self._highlighter.highlight(row)
        row.stylize_before(self.rich_style)
        strip = Strip(row.render(self.app.console)).extend_cell_length(width, self.rich_style)
        # Share the selection offsets/highlight convention with transcript text.
        from novacode_cli.tui.widgets import _selectable_line

        return _selectable_line(self, strip, 0, y)

    def render_lines(self, crop: Region) -> list[Strip]:
        """Let Textual decorate visible rows, then bound its historical cache."""
        lines = super().render_lines(crop)
        # Textual's style cache otherwise retains every row visited by scrolling.
        if self._pruning or self._closing or len(self._styles_cache._cache) > self._MAX_CACHED_ROWS:
            self._styles_cache.clear()
        return lines

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Copy text corresponding to the wrapped rows the user selected."""
        self._wrap_rows(self.size.width)
        text = "\n".join(line.plain[start:end].rstrip() for line, start, end in self._rows)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:  # noqa: ARG002
        """Invalidate painted rows so the selection highlight follows the user."""
        self.refresh()

    def on_unmount(self) -> None:
        """Release output even if a parent cache still references this widget."""
        self._lines = ()
        self._rows.clear()
        self._wrap_cache.clear()
        self._styles_cache.clear()
