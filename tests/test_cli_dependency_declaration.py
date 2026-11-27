"""The CLI dependency check cannot name a package pyproject no longer declares.

Regression: the langsmith re-lock edit accidentally dropped ``tavily-python``
from ``pyproject.toml``. ``check_cli_dependencies`` then killed every Nova
launch (including the process behind ``/sessions``) with the
"Missing required CLI dependencies" banner, because ``import tavily`` failed
in a fresh install. The check's package list must stay a subset of what the
project actually declares.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _checked_packages() -> dict[str, str]:
    """Map each module ``check_cli_dependencies`` imports to its package name."""
    source = (PROJECT / "novacode_cli" / "main.py").read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "check_cli_dependencies"
    )
    mapping: dict[str, str] = {}
    current: str | None = None
    for node in ast.walk(function):
        if isinstance(node, ast.Try):
            imports = [n for n in ast.walk(node) if isinstance(n, ast.Import)]
            if imports:
                current = imports[0].names[0].name
            for handler in node.handlers:
                if isinstance(handler.type, ast.Name) and handler.type.id == "ImportError":
                    names = [
                        n.value
                        for n in ast.walk(handler)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    ]
                    if current and names:
                        mapping[current] = names[0]
    return mapping


def test_every_checked_dependency_is_declared() -> None:
    declared = {
        dep.split(";")[0].split(">")[0].split("<")[0].split("=")[0].strip().lower()
        for dep in tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["dependencies"]
    }
    mapping = _checked_packages()
    assert "tavily" in mapping, "the check must still gate on tavily-python"
    for module, package in mapping.items():
        normalized = module.replace("-", "_").lower()
        matches = [d for d in declared if d.replace("-", "_") == normalized or normalized in d.replace("-", "_")]
        assert matches, f"check_cli_dependencies gates on {module!r} ({package}), which pyproject.toml no longer declares"
