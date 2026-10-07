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

    @property
    def content(self) -> Text:
        """Materialize the complete output only for an explicit consumer."""
        return Text("\n").join(self._lines)

    def replace_lines(self, lines: Iterable[Text]) -> None:
        """Replace references to logical lines without joining or rendering them."""
        self._lines = tuple(lines)
        retained = {id(line) for line in self._lines}
        self._wrap_cache = {
            key: rows for key, rows in self._wrap_cache.items() if key in retained
        }
        self._rows.clear()
        self._row_width = -1
        self.refresh(layout=True)

    def _wrap_rows(self, width: int) -> None:
        width = max(1, width)
        if self._row_width == width:
            return
        if self._wrap_width != width:
            self._wrap_cache.clear()
            self._wrap_width = width
        rows = []
        for line in self._lines:
            cached = self._wrap_cache.get(id(line))
            if cached is not None:
                rows.extend(cached)
                continue
            boundaries = divide_line(line.plain, width, fold=True) if self._wrap else []
            start = 0
            prepared = []
            for end in (*boundaries, len(line)):
                prepared.append((line, start, end))
                start = end
            self._wrap_cache[id(line)] = prepared
            rows.extend(prepared)
        self._rows = rows
        self._row_width = width

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
