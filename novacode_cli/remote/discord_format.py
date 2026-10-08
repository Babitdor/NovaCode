"""Readable Discord Markdown pages with balanced fenced code blocks."""

from __future__ import annotations

import re


def render(text: str, *, limit: int = 1900) -> list[str]:
    pages, page = [], ""
    fence = ""
    # Reserve space for closing/reopening code fences on every page.
    budget = limit - 90
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})([^\n]*)", line)
        if marker:
            line = "```" + (marker.group(2).strip()[:40] if not fence else "") + "\n"
            if len(page) + len(line) > budget:
                pages.append(page.rstrip() + ("\n```" if fence else ""))
                page = fence if fence else ""
        while len(page) + len(line) > budget:
            take = max(0, budget - len(page))
            page += line[:take]
            line = line[take:]
            pages.append(page.rstrip() + ("\n```" if fence else ""))
            page = fence if fence else ""
        page += line
        if marker:
            fence = "" if fence else line
    if page.strip():
        pages.append(page.rstrip() + ("\n```" if fence else ""))
    return pages or ["…"]
