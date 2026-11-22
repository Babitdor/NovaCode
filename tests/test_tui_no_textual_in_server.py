"""The server and the command layer must not import Textual (ADR-003).

`novacode_cli/tui/__init__.py` eagerly imports the Textual app, so *any*
`from novacode_cli.tui.x import y` drags the framework - and its 20 000-line
app - into the process that is supposed to be UI-agnostic. That is a real bug
this test has already caught once (`ui/statusline.py` and `ui/output_buffer.py`
both started life inside the TUI package).

Two checks, because they fail differently: the AST check names the offending
line, and the subprocess check proves the import graph end to end (a lazy
import inside a function would pass the AST check but still load Textual).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Every package that must stay framework-free.
PACKAGES = ("novacode_cli/server", "novacode_cli/commands", "novacode_cli/ui")

FORBIDDEN = ("textual", "novacode_cli.tui")


def _python_files(root: str) -> list[Path]:
    """Every module under *root*, sorted for a stable report."""
    return sorted((ROOT / root).rglob("*.py"))


def _module_name(node: ast.AST) -> str:
    """The module an import statement refers to."""
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    if isinstance(node, ast.Import):
        return ",".join(alias.name for alias in node.names)
    return ""


def _violations(path: Path) -> list[str]:
    """Forbidden imports in one module, as `line: module` strings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        module = _module_name(node)
        for name in module.split(","):
            forbidden = any(name == bad or name.startswith(f"{bad}.") for bad in FORBIDDEN)
            if forbidden:
                found.append(f"{Path(path).relative_to(ROOT)}:{node.lineno}: {name}")
    return found


@pytest.mark.parametrize("package", PACKAGES)
def test_no_framework_imports_in_the_neutral_layer(package: str) -> None:
    problems = [issue for path in _python_files(package) for issue in _violations(path)]
    assert not problems, "Textual crept into a framework-free package:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "module",
    [
        "novacode_cli.server.transport",
        "novacode_cli.server.host",
        "novacode_cli.server.frames",
        "novacode_cli.server.catalog",
        "novacode_cli.server.sessions",
        "novacode_cli.commands",
        "novacode_cli.ui.execution",
        "novacode_cli.ui.port_server",
    ],
)
def test_importing_it_does_not_load_textual(module: str) -> None:
    """The import graph, not just the source text: lazy imports count too."""
    code = f"import sys; import {module}; print('textual' in sys.modules)"
    done = subprocess.run(  # noqa: S603 — fixed interpreter and module, no shell
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert done.stdout.strip() == "False", f"{module} loaded Textual: {done.stderr[-400:]}"


def test_the_command_layer_never_reaches_for_the_textual_app() -> None:
    """`NovaApp` in the command layer is a fork point the port cannot honour."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_files("novacode_cli/commands")
        if "NovaApp" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"command modules reference the Textual app: {offenders}"
