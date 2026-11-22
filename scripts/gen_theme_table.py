"""Generate `novacode_cli/ui/theme_table.py` from the TUI's theme sources.

The server may not import Textual (ADR-003), but `/theme` has to list the same
themes the Textual UI offers. This script runs offline, reads
`novacode_cli/tui/themes.py` with `ast` and Textual's built-in registry, and
emits pure data: names and colours, no widgets, no framework. `--check` exits
non-zero when the checked-in table is stale, so a palette cannot drift.

Usage:
    uv run python scripts/gen_theme_table.py [--check]
"""

from __future__ import annotations

import argparse
import ast
import json
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
    "border",
)

VARIABLE_FIELDS = {"muted": "text-muted", "border": "border"}


def _literal(node: ast.AST) -> Any:  # noqa: ANN401 — an AST literal is any JSON scalar
    """Return the value of a constant AST node, or "" when it is not one."""
    if isinstance(node, ast.Constant):
        return node.value
    return ""


def _theme_call(node: ast.AST) -> dict[str, Any] | None:
    """Read one `Theme(...)` call into a row, or None when the shape is unknown."""
    if not isinstance(node, ast.Call):
        return None
    row: dict[str, Any] = dict.fromkeys(FIELDS, "")
    for keyword in node.keywords:
        if keyword.arg is None:
            continue
        value = _literal(keyword.value)
        if keyword.arg == "variables" and isinstance(keyword.value, ast.Dict):
            pairs = {
                str(_literal(key)): str(_literal(item))
                for key, item in zip(keyword.value.keys, keyword.value.values, strict=False)
            }
            for field, variable in VARIABLE_FIELDS.items():
                row[field] = pairs.get(variable, "")
        elif keyword.arg in row:
            row[keyword.arg] = value
    if not row["name"]:
        return None
    row["dark"] = bool(row["dark"] or False)
    return row


def _nova_themes() -> list[dict[str, Any]]:
    """Parse `NOVA_EXTRA_THEMES` out of the Textual theme module."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        if not any(getattr(target, "id", "") == "NOVA_EXTRA_THEMES" for target in targets):
            continue
        if not isinstance(node.value, ast.Tuple | ast.List):
            continue
        rows = [_theme_call(element) for element in node.value.elts]
        return [row for row in rows if row is not None]
    msg = "NOVA_EXTRA_THEMES not found in {SOURCE}"
    raise SystemExit(msg)


def _builtin_themes() -> list[dict[str, Any]]:
    """Read Textual's built-in themes; empty when Textual is not installed."""
    try:
        from textual.theme import BUILTIN_THEMES
    except Exception:  # noqa: BLE001 — the table still works without built-ins
        return []
    rows = []
    for name, theme in BUILTIN_THEMES.items():
        variables = dict(getattr(theme, "variables", {}) or {})
        rows.append(
            {
                "name": name,
                "dark": bool(getattr(theme, "dark", False)),
                "primary": str(getattr(theme, "primary", "")),
                "secondary": str(getattr(theme, "secondary", "")),
                "accent": str(getattr(theme, "accent", "")),
                "success": str(getattr(theme, "success", "")),
                "warning": str(getattr(theme, "warning", "")),
                "error": str(getattr(theme, "error", "")),
                "background": str(getattr(theme, "background", "")),
                "foreground": str(getattr(theme, "foreground", "")),
                "surface": str(getattr(theme, "surface", "")),
                "panel": str(getattr(theme, "panel", "")),
                "muted": str(variables.get("text-muted", "")),
                "border": str(variables.get("border", "")),
            }
        )
    return rows


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
    rows = _builtin_themes() + _nova_themes()
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        unique.append(row)
    return render(unique), len(unique)


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
