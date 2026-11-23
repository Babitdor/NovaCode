"""Markdown heading index and section reads for long documents.

`file_outline` answers the question a paged `read_file` cannot: *what is in
this document and where?* The outline lists every heading with its line
number; a `section` query returns one section's text, matching headings
case-insensitively so ``section="run locally"`` finds ``## Run it locally``.
A section query that matches nothing returns the outline itself, so a wrong
heading guess costs one call, not a round trip of blind offsets.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from langchain_core.tools import BaseTool, tool

if TYPE_CHECKING:
    from collections.abc import Callable

from novacode_cli.security.paths import resolve_beneath

#: Section bodies are capped like tool results: one huge section can't flood
#: the transcript any more than one huge file read can.
SECTION_TEXT_LIMIT = 20_000

_MAX_FILE_CHARS = 2 * 1024 * 1024

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _outline(lines: list[str]) -> list[tuple[int, int, str]]:
    """`(0-indexed line, level, text)` for every ATX heading outside fences."""
    out: list[tuple[int, int, str]] = []
    fence = ""
    for index, line in enumerate(lines):
        if fence:
            if line.strip().startswith(fence):
                fence = ""
            continue
        opening = _FENCE.match(line)
        if opening:
            fence = opening.group(1)
            continue
        heading = _HEADING.match(line)
        if heading:
            out.append((index, len(heading.group(1)), heading.group(2).strip()))
    return out


def _outline_text(path: str, headings: list[tuple[int, int, str]], total: int) -> str:
    if not headings:
        return f"No Markdown headings in {path} ({total} lines)."
    index = "\n".join(f"{line + 1}: {'#' * level} {text}" for line, level, text in headings)
    return f"@@ outline: {len(headings)} headings · {total} lines @@\n{index}"


def _section_text(
    lines: list[str], total: int, start: int, level: int
) -> str:
    """One section: its heading line through the next same-or-higher level."""
    end = total
    for line, heading_level, _ in _outline(lines):
        if line > start and heading_level <= level:
            end = line
            break
    body = "\n".join(lines[start:end])
    header = f"@@ lines {start + 1}-{end} of {total} @@"
    if len(body) > SECTION_TEXT_LIMIT:
        body = body[:SECTION_TEXT_LIMIT] + f"\n… ({len(body) - SECTION_TEXT_LIMIT} more characters)"
    return f"{header}\n{body}"


def build_file_outline_tool(root_provider: Callable[[], Path]) -> BaseTool:
    """Build `file_outline`, confined to the workspace root like `read_file`."""

    @tool
    def file_outline(file_path: str, section: str | None = None) -> str:
        """List a text file's Markdown heading outline with line numbers, or read one section.

        Use before paging through a long Markdown, README or docs file. Call with
        just `file_path` for the outline; then call again with `section` set to
        heading text (case-insensitive, partial match) to read that section. A
        section that matches nothing returns the outline, so a wrong heading
        guess is self-correcting.
        """
        root = Path(root_provider()).resolve()
        try:
            target = resolve_beneath(root, file_path)
        except Exception as exc:  # noqa: BLE001 — path errors are tool results, not crashes
            return f"Error: {file_path} is outside the workspace ({exc})."
        if not target.is_file():
            return f"Error: file not found: {file_path}. Discover the exact path with glob or ls."
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Error: cannot read {file_path}: {exc}"
        if "\x00" in text[:8000]:
            return f"Error: {file_path} is not a text file."
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS]
        lines = text.splitlines()
        headings = _outline(lines)
        if not section:
            return _outline_text(file_path, headings, len(lines))
        query = section.strip().casefold()
        if not query:
            return _outline_text(file_path, headings, len(lines))
        words = query.split()

        def matches(heading_text: str) -> bool:
            casefolded = heading_text.casefold()
            return query in casefolded or all(word in casefolded for word in words)

        found = [h for h in headings if matches(h[2])]
        if not found:
            return (
                f"No section matching '{section}' in {file_path}.\n"
                + _outline_text(file_path, headings, len(lines))
                + "\nRetry with section set to one of the headings above."
            )
        if len(found) > 1:
            listing = "\n".join(f"{line + 1}: {'#' * level} {text}" for line, level, text in found)
            return (
                f"'{section}' matches {len(found)} headings in {file_path}; "
                f"retry with the exact heading text:\n{listing}"
            )
        line, level, _ = found[0]
        return _section_text(lines, len(lines), line, level)

    return file_outline
