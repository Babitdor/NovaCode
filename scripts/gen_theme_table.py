"""Generate `novacode_cli/ui/theme_table.py` from the TUI's theme sources.

The server may not import Textual (ADR-003), but `/theme` has to list the same
themes the Textual UI offers. This script runs offline, reads
`novacode_cli/tui/themes.py` and Textual's built-in registry, and
emits pure data: names and colours, no widgets, no framework. `--check` exits
non-zero when the checked-in table is stale, so a palette cannot drift.

Usage:
    uv run python scripts/gen_theme_table.py [--check]
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "novacode_cli" / "tui" / "themes.py"
TARGET = ROOT / "novacode_cli" / "ui" / "theme_table.py"

# Fields every row carries, in emission order. `muted` and `border` come from
# the theme's `variables` mapping and are empty when the theme sets neither.
FIELDS: tuple[str, ...] = (
    "name",
    "dark",
    "primary",
    "secondary",
    "accent",
    "success",
    "warning",
    "error",
    "background",
    "foreground",
    "surface",
    "panel",
    "muted",
    "activity",
    "border",
)

VARIABLE_FIELDS = {"muted": "text-muted", "border": "border", "activity": "nova-activity"}


def _theme_rows() -> list[dict[str, Any]]:
    """Resolve the same built-in themes and Nova overrides used at runtime."""
    from textual.theme import BUILTIN_THEMES

    source = runpy.run_path(str(SOURCE))
    themes = dict(BUILTIN_THEMES)
    themes.update(
        (theme.name, theme)
        for theme in (*source["NOVA_LIGHT_THEMES"], *source["NOVA_EXTRA_THEMES"])
    )
    return [
        {
            field: theme.variables.get(VARIABLE_FIELDS[field], "")
            if field in VARIABLE_FIELDS
            else getattr(theme, field)
            for field in FIELDS
        }
        for theme in themes.values()
    ]


def _python(value: Any) -> str:  # noqa: ANN401 — a row value is a str or a bool
    """Render one value as Python source, matching the project's quote style."""
    if isinstance(value, bool):
        return "True" if value else "False"
    return json.dumps(str(value))


def render(rows: list[dict[str, Any]]) -> str:
    """Render the generated module for `rows`, sorted by theme name."""
    out = [
        '"""Theme names and colours, generated from the Textual UI. Do not edit.',
        "",
        "Run `uv run python scripts/gen_theme_table.py` after changing a palette,",
        "and `--check` in CI to prove this table is current.",
        "",
        "The server cannot import Textual, so `/theme` and the ratatui client read",
        "this table instead: the same names, the same colours, one source.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class ThemeEntry:",
        '    """One selectable theme: its name, brightness and palette."""',
        "",
    ]
    out.extend(f"    {field}: {'bool' if field == 'dark' else 'str'}" for field in FIELDS)
    out.extend(["", "", "THEMES: tuple[ThemeEntry, ...] = ("])
    for row in sorted(rows, key=lambda item: str(item["name"])):
        out.append("    ThemeEntry(")
        out.extend(f"        {field}={_python(row[field])}," for field in FIELDS)
        out.append("    ),")
    out.extend(
        [
            ")",
            "",
            "",
            "BY_NAME: dict[str, ThemeEntry] = {theme.name: theme for theme in THEMES}",
            "",
            "# The theme a session starts with when the user has never chosen one.",
            'DEFAULT_THEME = "textual-dark" if "textual-dark" in BY_NAME else THEMES[0].name',
            "",
        ]
    )
    return "\n".join(out)


def build() -> tuple[str, int]:
    """Return the rendered table and its row count."""
    rows = _theme_rows()
    return render(rows), len(rows)


def main(argv: list[str] | None = None) -> int:
    """Run the generator, or verify the checked-in table with `--check`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args(argv)
    text, count = build()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            sys.stderr.write("theme table is stale: run scripts/gen_theme_table.py\n")
            return 1
        sys.stdout.write(f"theme table ok: {count} themes\n")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {TARGET.relative_to(ROOT)} ({count} themes)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
