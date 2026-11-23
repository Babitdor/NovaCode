"""Every first-party import in the source must resolve — including deferred ones.

A tool removal once broke a *function-level* ``from
novacode_cli.tools.web_tools import _DOCS_SITES``: grepping for the removed
tool name missed it, because the hidden consumer imported a module-level
constant instead. An import-time sweep misses deferred imports too. This test
walks the AST of every ``novacode_cli`` source file — function bodies
included — and verifies each first-party ``import`` resolves at runtime.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

import novacode_cli

PACKAGE_ROOT = Path(novacode_cli.__file__).resolve().parent
PACKAGE = novacode_cli.__name__
#: Modules whose optional third-party dependencies may legitimately be absent
#: (``fastapi`` is not in the default dev sync). A missing THIRD-PARTY module
#: skips; a missing FIRST-PARTY name fails.
_OPTIONAL_THIRD_PARTY = {"fastapi", "uvicorn"}


def _python_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _package_name(path: Path) -> str:
    """File's package: ``<root>/tools/x.py`` and ``<root>/tools/__init__.py``
    are both → ``novacode_cli.tools`` (the stem never belongs to the package).
    """
    parts = list(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
    return ".".join(parts[:-1])


def _absolute(level: int, module: str | None, package: str) -> str:
    """Resolve a relative import to an absolute module name."""
    base = package.split(".")[: len(package.split(".")) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def _type_checking_spans(tree: ast.Module) -> list[tuple[int, int]]:
    """Line spans of ``if TYPE_CHECKING:`` blocks — those imports are static
    hints, not runtime requirements."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            guard = (
                (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                or (
                    isinstance(test, ast.Attribute)
                    and test.attr == "TYPE_CHECKING"
                )
            )
            if guard and node.end_lineno:
                spans.append((node.lineno, node.end_lineno))
    return spans


def _first_party_imports(
    path: Path, source: str, package: str | None = None
) -> list[tuple[str, list[str], int]]:
    """`(absolute_module, [imported names], lineno)` for every first-party import."""
    package = package or _package_name(path)
    tree = ast.parse(source, filename=str(path))
    spans = _type_checking_spans(tree)
    out: list[tuple[str, list[str], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(lo <= node.lineno <= hi for lo, hi in spans):
                continue
            if node.level:
                module = _absolute(node.level, node.module, package)
            else:
                module = node.module or ""
            if module == PACKAGE or module.startswith(PACKAGE + "."):
                out.append((module, [alias.name for alias in node.names], node.lineno))
        elif isinstance(node, ast.Import):
            if any(lo <= node.lineno <= hi for lo, hi in spans):
                continue
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    out.append((alias.name, [], node.lineno))
    return out


def _missing_third_party(exc: BaseException) -> bool:
    """True when the failure is an absent third-party dependency, not our code."""
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        top = exc.name.split(".")[0]
        if top in _OPTIONAL_THIRD_PARTY:
            return True
        try:
            return importlib.util.find_spec(top) is None
        except (ImportError, ValueError):
            return False
    return False


def test_every_first_party_import_resolves() -> None:
    problems: list[str] = []
    skipped: list[str] = []
    for path, source in _python_sources().items():
        package = _package_name(path)
        for module_name, names, lineno in _first_party_imports(path, source):
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001
                if _missing_third_party(exc):
                    skipped.append(f"{path.name}:{lineno} {module_name} ({exc})")
                else:
                    problems.append(f"{path.name}:{lineno} importing {module_name}: {exc!r}")
                continue
            for name in names:
                if name == "*":
                    continue
                if hasattr(module, name):
                    continue
                # A name may be a submodule that the parent has not imported yet.
                try:
                    importlib.import_module(f"{module_name}.{name}")
                except Exception as exc:  # noqa: BLE001
                    if _missing_third_party(exc):
                        skipped.append(
                            f"{path.name}:{lineno} {module_name}.{name} ({exc})"
                        )
                    else:
                        problems.append(
                            f"{path.name}:{lineno} '{name}' does not exist in {module_name}"
                        )
    if skipped:
        print(f"\nskipped {len(skipped)} module(s) with absent optional dependencies:")
        for entry in skipped:
            print(f"  {entry}")
    assert not problems, "broken first-party imports:\n" + "\n".join(problems)


def test_the_checker_finds_a_planted_broken_import() -> None:
    """Guard the guard: a function-level import of a missing name must be caught."""
    source = "def later():\n    from novacode_cli.session.history_tree import NoSuchName\n"
    imports = _first_party_imports(
        PACKAGE_ROOT / "session" / "_probe.py", source, package="novacode_cli.session"
    )
    assert imports == [("novacode_cli.session.history_tree", ["NoSuchName"], 2)]
    module = importlib.import_module("novacode_cli.session.history_tree")
    assert not hasattr(module, "NoSuchName"), "the planted name must fail the check"


def test_the_checker_resolves_relative_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.tools import web_tools  # noqa: F401

    source = "from .web_tools import duckduckgo_search\nfrom ..tools import fetch_url\n"
    path = PACKAGE_ROOT / "tools" / "_checker_probe.py"
    try:
        monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: source, raising=False)
        imports = _first_party_imports(path, source)
        assert (PACKAGE + ".tools.web_tools", ["duckduckgo_search"], 1) in imports
        assert (PACKAGE + ".tools", ["fetch_url"], 2) in imports
    finally:
        path.unlink(missing_ok=True)
