"""Generate changelog/index.html from per-version Markdown sources.

Reads one ``CHANGELOG-v<major>.<minor>.<patch>.md`` file from the upstream
NovaCode repo per release, produces an ordered <div class="changelog-entry">
block inside the markers on ``changelog/index.html``.

Usage
-----
    python tools/make_changelog_page.py [--source-dir DIR] [--dry-run]

Without --source-dir the script downloads changelog sources from
https://github.com/Babitdor/NovaCode/contents/changelog using the GitHub API.
With --source-dir it reads from local files instead.

Source Markdown formats
-----------------------
Format A (v1.0.0 – v1.0.48):
    # Changelog - v1.0.X

    ## New Features / Improvements / Bug Fixes / Technical Changes
    ### Sub-heading
    - Detail line

Format B (v1.0.49+):
    # NovaCode v1.0.X

    ## Added / Fixed / Validation and limits
    - Detail line

Review files (skipped): HEADLESS-REVIEW*, PIPE-REVIEW*, TABS-PERFORMANCE*
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from urllib.request import Request, urlopen
except ImportError:
    urlopen = None


REPO_URL = "https://api.github.com/repos/Babitdor/NovaCode/contents/changelog"
CHANGELOG_INDEX = Path(__file__).resolve().parent.parent / "changelog" / "index.html"
MARKER_START = "<!-- generated:changelog-releases -->"
MARKER_END = "<!-- /generated:changelog-releases -->"
SUMMARY_MARKER_START = "<!-- generated:changelog-summary -->"
SUMMARY_MARKER_END = "<!-- /generated:changelog-summary -->"
SKIPPED_PREFIXES = ("HEADLESS-", "PIPE-", "TABS-")

# Pattern for "# NovaCode v1.0.X", "# NovaCode 1.0.X" or "# Changelog - v1.0.X"
# The leading "v" after "NovaCode" is optional (older files drop it), and a
# trailing em-dash title ("# NovaCode 1.0.14 - Background commands") is ignored.
_VERSION_RE = re.compile(
    r"^#\s+(?:Changelog\s+-|NovaCode\s+v?)(\d+\.\d+\.\d+)", re.IGNORECASE
)
# Also handle "# Changelog – vX.Y.Z" or "# CHANGELOG - v1.0.X" style headers
_VERSION_RE_ALT = re.compile(
    r"^#\s*[Cc]hangelog[\s\-–]+v(\d+\.\d+\.\d+)", re.IGNORECASE
)
# Pattern for "2025-07-14" (ISO date in the original HTML date field)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

@dataclass
class ReleaseEntry:
    """One parsed release, ready for template rendering."""
    version: str
    date: str = ""
    title: str = ""
    description: str = ""
    details_html: str = ""
    sha: str = ""


def _fetch_sources_github() -> list[dict[str, Any]]:
    """Download the directory listing via the GitHub REST API."""
    if urlopen is None:
        raise SystemExit("urlopen unavailable; pass --source-dir with local files")
    req = Request(REPO_URL, headers={"Accept": "application/vnd.github.v3+json"})
    raw = urlopen(req, timeout=30).read().decode()
    return json.loads(raw)


def _parse_version_from_filename(name: str) -> str | None:
    """Extract vX.Y.Z from CHANGELOG-v1.0.50.md style names."""
    m = re.fullmatch(r"CHANGELOG-v(\d+\.\d+\.\d+)\.md", name, re.IGNORECASE)
    return m.group(1) if m else None


def _version_key(version: str) -> tuple[int, int, int]:
    """Sort key: (major, minor, patch) for ascending order."""
    parts = version.split(".")
    return tuple(int(p) for p in parts)


def _date_from_md(content: str) -> str:
    """Return an ISO date found in the source, or '' when none is present.

    The upstream changelog markdown carries no release dates, so this is
    usually empty; only a real ``YYYY-MM-DD`` token is trusted.
    """
    m = _DATE_RE.search(content)
    return m.group(1) if m else ""


def title_for(version: str) -> str:
    """Fallback title from the version itself."""
    return f"Release v{version}"


def _parse_markdown(content: str) -> dict[str, Any]:
    """Parse a changelog Markdown file into structured fields."""
    lines = content.strip().splitlines()

    # --- Title / Version ---
    version = None
    title = ""
    rest_lines: list[str] = []
    started = False
    for line in lines:
        if not started:
            vm = _VERSION_RE.match(line) or _VERSION_RE_ALT.match(line)
            if vm:
                version = vm.group(1)
                started = True
                continue
            # If it starts with something like "# NovaCode v1.0.52"
            # skip the header line
            continue
        else:
            rest_lines.append(line)

    if not version:
        return {}

    # --- Build sections for HTML ---
    sections: list[tuple[str, list[str]]] = []
    current_section: str | None = None
    current_items: list[str] = []
    # Bullets that appear before the first "## Section" heading (flat-list
    # releases such as v1.0.10/v1.0.14) are kept as a preamble list.
    preamble_items: list[str] = []
    in_preamble = True

    def flush_section() -> None:
        nonlocal current_section, current_items
        if current_section:
            sections.append((current_section, current_items))
        current_section = None
        current_items = []

    for line in rest_lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_preamble = False
            flush_section()
            current_section = stripped[3:].strip("`").strip()
        elif stripped.startswith("### "):
            in_preamble = False
            flush_section()
            # Make subheading an inline title for next items
            current_section = stripped[4:].strip("`").strip()
        elif stripped.startswith("- ") or stripped.startswith("* "):
            # Strip exactly one list-marker char + space, preserving bold markers inside
            item_text = stripped[2:].lstrip()
            if in_preamble:
                preamble_items.append(item_text)
            else:
                current_items.append(item_text)
        elif stripped and not stripped.startswith("#"):
            # Continuation of the previous item (markdown soft-wrapped bullet).
            # Join to the last item in the active container instead of starting
            # a new list entry.
            active = preamble_items if in_preamble else current_items
            if active:
                active[-1] = (active[-1] + " " + stripped).strip()
            else:
                current_items.append(stripped)
        elif not stripped:
            # Blank line between paragraphs keeps accumulating until real content
            pass

    flush_section()

    # --- Description: first bullet text (preamble preferred, else first section) ---
    # Plain escaped text for the inline summary span; markdown markers stripped.
    first_item = ""
    if preamble_items:
        first_item = preamble_items[0]
    else:
        for _sec_name, sec_items in sections:
            if sec_items:
                first_item = sec_items[0]
                break
    description = ""
    if first_item:
        clean = re.sub(r"\*\*(.+?)\*\*", r"\1", first_item)   # strip bold markers
        clean = re.sub(r"`(.+?)`", r"\1", clean)               # strip inline code backticks
        clean = clean.strip()
        description = clean

    # Build detail content as proper HTML (no intermediate markdown).
    parts: list[str] = []
    if preamble_items:
        lis = []
        for item in preamble_items:
            text = _html_escape(item)
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
            text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
            lis.append(f"<li>{text}</li>")
        parts.append("<ul>\n" + "\n".join(lis) + "\n</ul>")
    for sec_name, items in sections:
        if not items:
            continue
        heading = _html_escape(sec_name)
        lis = []
        for item in items:
            text = _html_escape(item)
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
            text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
            lis.append(f"<li>{text}</li>")
        ul = "\n".join(lis)
        parts.append(f"<h3>{heading}</h3>\n<ul>\n{ul}\n</ul>")
    details_html = "\n".join(parts)

    return {
        "version": version,
        "title": title_for(version),
        "description": description,
        "details_html": details_html,
    }


def _markdown_to_html(text: str) -> str:
    """Convert simple Markdown (headings, bold, lists) to HTML. Covers the changelog subset only."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---" or stripped == "***":
            out.append("<hr>")
            continue
        m = re.match(r"^###\s+(.+)$", stripped)
        if m:
            out.append(f"<h4>{_html_escape(m.group(1))}</h4>")
            continue
        m = re.match(r"^####\s+(.+)$", stripped)
        if m:
            out.append(f"<h5>{_html_escape(m.group(1))}</h5>")
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            # Strip exactly one list-marker char + space, preserving any bold markers inside
            inner = stripped[2:] if len(stripped) >= 2 else stripped
            inner = _html_escape(inner.strip())
            inner = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", inner)
            inner = re.sub(r"`(.+?)`", r"<code>\1</code>", inner)
            out.append(f"<li>{inner}</li>")
            continue
        if stripped:
            escaped = _html_escape(stripped)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
            out.append(f"<p>{escaped}</p>")

    # Wrap contiguous li elements in ul
    result: list[str] = []
    in_list = False
    for line in out:
        if line.startswith("<li>"):
            if not in_list:
                result.append("<ul>")
                in_list = True
            result.append(line)
        else:
            if in_list:
                result.append("</ul>")
                in_list = False
            result.append(line)
    if in_list:
        result.append("</ul>")

    return "\n".join(result)


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def parse_release_entries(
    filename: str | None = None,
    content: str | None = None,
) -> ReleaseEntry | None:
    """Public entry-point: given a filename or raw content string, produce a ReleaseEntry."""
    if filename:
        fp = Path(filename)
        if fp.exists():
            text = fp.read_text(encoding="utf-8")
        else:
            return None
    elif content is not None:
        text = content
    else:
        return None

    parsed = _parse_markdown(text)
    if not parsed or not parsed.get("version"):
        return None

    return ReleaseEntry(
        version=parsed["version"],
        date=_date_from_md(text),
        title=parsed.get("title", f"Release v{parsed['version']}"),
        description=parsed.get("description", f"Release v{parsed['version']}"),
        details_html=parsed.get("details_html", ""),
        sha=hashlib.sha1(f"blob {len(text.encode('utf-8'))}\0".encode() + text.encode('utf-8')).hexdigest(),
    )


def render_entry(entry: ReleaseEntry) -> str:
    """Render a single changelog entry block matching the existing HTML structure."""
    anchor = "v" + entry.version.replace(".", "-")
    time_html = f'<time datetime="{_html_escape(entry.date)}">{_html_escape(entry.date)}</time>' if entry.date else ""
    return (
        f'<details class="changelog-entry" id="{anchor}" data-version="{entry.version}" data-sha="{entry.sha}">\n'
        f'<summary class="entry-toggle"><h2 class="version">v{entry.version}</h2>{time_html}'
        f'<span class="release-summary">{_html_escape(entry.description)}</span></summary>\n'
        f'<div class="release-content">{entry.details_html}</div>\n</details>'
    )


def build_changelog_html(entries: list[ReleaseEntry]) -> str:
    """Build the full <section id="releases">...</section> fragment."""
    blocks = [render_entry(e) for e in entries]
    return "\n".join(blocks)


def build_summary_html(entries: list[ReleaseEntry]) -> str:
    """Build the one-line summary shown above the release list.

    ``entries`` are newest-first, so the first is the latest release and the
    last is the earliest.
    """
    count = len(entries)
    latest = entries[0].version
    earliest = entries[-1].version
    return (
        f"<p class=\"lede\">{count} releases &middot; "
        f"v{_html_escape(earliest)} &rarr; v{_html_escape(latest)} &middot; "
        "from the repository's <code>changelog/</code> directory</p>"
    )


def main() -> None:
    # The Windows console defaults to cp1252; changelog content contains
    # unicode (arrows, etc.). Force UTF-8 so dry-run printing never raises.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dir", help="local dir containing CHANGELOG-v*.md files")
    ap.add_argument("--output", default=str(CHANGELOG_INDEX), help="path to changelog/index.html")
    ap.add_argument("--dry-run", action="store_true", help="print generated HTML to stdout only")
    args = ap.parse_args()

    # Collect source files
    sources: list[tuple[str, str]] = []  # (filename, content)
    if args.source_dir:
        src_dir = Path(args.source_dir)
        if not src_dir.exists():
            raise SystemExit(f"--source-dir {src_dir} does not exist")
        for f in sorted(src_dir.iterdir()):
            if f.suffix != ".md":
                continue
            name = f.name
            # Skip review-type files
            if any(name.startswith(p) for p in SKIPPED_PREFIXES):
                continue
            if _parse_version_from_filename(name) is None:
                continue
            sources.append((name, f.read_text(encoding="utf-8")))
    elif urlopen is not None:
        api_files = _fetch_sources_github()
        for fi in api_files:
            name = fi["name"]
            if any(name.startswith(p) for p in SKIPPED_PREFIXES):
                continue
            if _parse_version_from_filename(name) is None:
                continue
            # Download the raw file
            url = fi["download_url"]
            req = Request(url)
            raw = urlopen(req, timeout=30).read().decode("utf-8")
            sources.append((name, raw))
    else:
        raise SystemExit("No --source-dir and no network available")

    if not sources:
        print("No changelog source files found.")
        raise SystemExit(1)

    # Parse all entries
    entries: list[ReleaseEntry] = []
    for name, content in sources:
        entry = parse_release_entries(content=content)
        if entry:
            entries.append(entry)

    if not entries:
        print("No valid changelog entries parsed.")
        raise SystemExit(1)

    # Sort newest-first (latest release first in the list).
    entries.sort(key=lambda e: _version_key(e.version), reverse=True)

    html_fragment = build_changelog_html(entries)
    summary_html = build_summary_html(entries)

    if args.dry_run:
        print(html_fragment)
        print(f"# Generated {len(entries)} entries "
              f"({entries[-1].version} - {entries[0].version})")
        return

    # Write into the changelog index file (both generated regions).
    target = Path(args.output)
    if not target.exists():
        raise SystemExit(f"{target} does not exist; cannot update")

    body = target.read_text(encoding="utf-8")

    def _replace_region(body: str, start_marker: str, end_marker: str,
                        replacement: str) -> str:
        start = body.find(start_marker)
        end = body.rfind(end_marker)
        if start < 0 or end < 0 or end < start:
            raise SystemExit(
                f"{target.name} missing markers. "
                f"Need '{start_marker}' ... '{end_marker}'"
            )
        return body[: start + len(start_marker)] + "\n" + replacement + "\n" + body[end:]

    body = _replace_region(body, SUMMARY_MARKER_START, SUMMARY_MARKER_END, summary_html)
    body = _replace_region(body, MARKER_START, MARKER_END, html_fragment)
    target.write_text(body, encoding="utf-8")
    print(f"Wrote {len(entries)} entries -> {target}")


if __name__ == "__main__":
    main()
