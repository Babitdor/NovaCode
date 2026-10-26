"""Responsive, numbered Before/After previews for completed file edits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import zip_longest
from typing import TYPE_CHECKING

from rich.text import Text
from textual.color import Color
from textual.widgets import Static

from novacode_cli.ui.diff_highlight import highlight_line, lexer_for_path

if TYPE_CHECKING:
    from textual import events
    from textual.app import RenderResult

DIFF_PREVIEW_STYLES = (
    ("Classic · unified diff", "classic"),
    ("Side by side · experimental", "side-by-side"),
)
_MIN_SPLIT_WIDTH = 80
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def diff_preview_style(value: object) -> str:
    """Keep missing or invalid preferences on the existing Classic preview."""
    return "side-by-side" if value == "side-by-side" else "classic"


@dataclass(frozen=True)
class DiffLine:
    """One source line with its authoritative hunk line number and marker."""

    number: int
    code: str
    marker: str = " "


@dataclass(frozen=True)
class DiffRow:
    """Aligned source lines, or an annotation spanning the comparison."""

    before: DiffLine | None = None
    after: DiffLine | None = None
    note: str = ""


def diff_rows(diff: str, max_lines: int = 500) -> list[DiffRow]:
    """Align change blocks in source order without inventing context or numbers."""
    rows: list[DiffRow] = []
    removed: list[DiffLine] = []
    added: list[DiffLine] = []
    old, new = 1, 1
    in_hunk = False

    def flush() -> None:
        rows.extend(DiffRow(left, right) for left, right in zip_longest(removed, added))
        removed.clear()
        added.clear()

    lines = diff.splitlines()
    for line in lines[:max_lines]:
        match = _HUNK.match(line)
        if match:
            flush()
            old, new = int(match[1]), int(match[2])
            in_hunk = True
            rows.append(DiffRow(note=line))
        elif line.startswith(("diff --git ", "index ")):
            flush()
            in_hunk = False
            rows.append(DiffRow(note=line))
        elif not in_hunk and line.startswith(("--- ", "+++ ")):
            rows.append(DiffRow(note=line))
        elif line.startswith("-"):
            removed.append(DiffLine(old, line[1:], "-"))
            old += 1
        elif line.startswith("+"):
            added.append(DiffLine(new, line[1:], "+"))
            new += 1
        elif line.startswith(" "):
            flush()
            rows.append(DiffRow(DiffLine(old, line[1:]), DiffLine(new, line[1:])))
            old += 1
            new += 1
        else:
            flush()
            rows.append(DiffRow(note=line))
    flush()
    if len(lines) > max_lines:
        rows.append(DiffRow(note=f"… {len(lines) - max_lines} more lines"))
    return rows


class DiffPreview(Static):
    """Retain both presentations so appearance changes apply to existing edits."""

    def __init__(self, *, classes: str | None = None) -> None:
        """Start as a normal tool body until a recorded edit arrives."""
        super().__init__("", classes=classes)
        self._classic: Text | None = None
        self._rows: list[DiffRow] = []
        self._path: str | None = None
        self.preview_style = "classic"
        self._highlighted: dict[tuple[str, bool, str], Text] = {}

    def set_diff(self, diff: str, path: str | None, classic: Text, style: str) -> None:
        """Bind the recorded diff; rendering never reads a source file."""
        self._classic = classic
        self._rows = diff_rows(diff)
        self._path = path
        self._highlighted.clear()
        self.set_preview_style(style)

    def set_preview_style(self, style: str) -> None:
        """Reflow this preview immediately, including previously completed edits."""
        self.preview_style = diff_preview_style(style)
        self.refresh(layout=True)

    def on_resize(self, _event: events.Resize) -> None:
        """Rewrap each side to its canvas or use Classic on small terminals."""
        self.refresh(layout=True)

    def render(self) -> RenderResult:
        """Draw aligned source rows with independent numbering and syntax colors."""
        if self._classic is None:
            return super().render()
        width = self.content_size.width
        if self.preview_style != "side-by-side" or width < _MIN_SPLIT_WIDTH:
            return self._classic

        from novacode_cli.tui.palette import cached_palette

        palette = cached_palette(self.app.current_theme)
        dark = self.app.current_theme.dark
        left_width = (width - 3) // 2
        right_width = width - 3 - left_width
        digits = max(
            (
                len(str(line.number))
                for row in self._rows
                for line in (row.before, row.after)
                if line
            ),
            default=1,
        )
        gutter = digits + 3
        removed_bg = Color.parse(palette.surface).blend(Color.parse(palette.error), 0.18).hex
        added_bg = Color.parse(palette.surface).blend(Color.parse(palette.primary), 0.18).hex
        lexer = lexer_for_path(self._path)

        def cell(line: DiffLine | None, cell_width: int) -> list[Text]:
            if line is None:
                return [Text(" " * cell_width)]
            key = (line.code, dark, palette.text)
            if key not in self._highlighted:
                code = Text(style=palette.text)
                for value, style in highlight_line(
                    line.code, lexer, theme="monokai" if dark else "default"
                ):
                    code.append(value, style=style)
                code.expand_tabs(4)
                self._highlighted[key] = code
            wrapped = self._highlighted[key].wrap(
                self.app.console, max(1, cell_width - gutter), overflow="fold"
            ) or [Text()]
            rendered: list[Text] = []
            for index, code in enumerate(wrapped):
                prefix = Text(
                    f"{line.number:>{digits}} {line.marker} " if index == 0 else " " * gutter,
                    style=palette.muted,
                )
                prefix.append_text(code)
                prefix.pad_right(max(0, cell_width - prefix.cell_len))
                if line.marker in {"-", "+"}:
                    prefix.stylize(f"on {removed_bg if line.marker == '-' else added_bg}")
                rendered.append(prefix)
            return rendered

        output = Text()
        output.append("Before".ljust(left_width) + " │ " + "After\n", style=palette.muted)
        for row in self._rows:
            if row.note:
                note = Text(row.note, style=palette.muted)
                note.truncate(width, overflow="ellipsis")
                output.append_text(note)
                output.append("\n")
                continue
            left = cell(row.before, left_width)
            right = cell(row.after, right_width)
            for before, after in zip_longest(left, right):
                output.append_text(before if before is not None else Text(" " * left_width))
                output.append(" │ ", style=palette.faint)
                output.append_text(after if after is not None else Text(" " * right_width))
                output.append("\n")
        return output
