"""The parity ledger is the audit: keep it honest or fail a test.

`rust/LEDGER.md` claims which modules are ported, served server-side, or still
`not-started`. Three ways that claim can rot:

* a row can say `ported` while naming no test that exists, which makes "ported"
  a word instead of a check;
* the default UI can flip to ratatui while rows are still `not-started`, which
  would ship a client whose features silently answer "not ported";
* a row can name a module that has moved or been deleted.

This module fails on all three. It imports nothing from the TUI: the ledger is
data and the checks are about text and paths.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "rust" / "LEDGER.md"
CLI_ARGS = ROOT / "novacode_cli" / "cli_args.py"
TUI_DIR = ROOT / "novacode_cli" / "tui"

STATES = ("not-started", "ported", "server-side", "n/a")

# A row must name at least one repository path to count as evidence. Matches
# tests/x.py, rust/nova-tui/src/y.rs and novacode_cli/z.py alike.
PATH_RE = re.compile(r"(?:tests|rust|novacode_cli|scripts)/[\w./-]+\.(?:py|rs|md|jsonl|txt)")


def _rows() -> Iterator[tuple[int, list[str]]]:
    """Yield every table row that carries a ledger state.

    Returns:
        An iterator of `(line number, cells)` for rows whose cells include a
        known state.
    """
    for number, line in enumerate(LEDGER.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 3 and any(cell in STATES for cell in cells):
            yield number, cells


def _state(cells: list[str]) -> str:
    """The state cell of a row.

    Args:
        cells: The row's cells.

    Returns:
        The state, as written in the ledger.
    """
    for cell in cells:
        if cell in STATES:
            return cell
    return "unknown"


def _resolve(node: ast.AST, fallbacks: dict[str, str]) -> str:
    """Statically resolve a small default expression to a string.

    Args:
        node: The expression node.
        fallbacks: Module-level string constants by name.

    Returns:
        The resolved string, or `"unknown"` when the expression is dynamic.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return fallbacks.get(node.id, "unknown")
    return "unknown"


def _collect_fallbacks(tree: ast.Module) -> dict[str, str]:
    """Find the module-level string constants the --ui default is built from.

    Args:
        tree: Parsed novacode_cli/cli_args.py.

    Returns:
        Name to the last string in its assignment, which is the fallback of an
        "os.environ.get(...) or x" expression.
    """
    fallbacks: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.BoolOp) and value.values:
            last = value.values[-1]
            if isinstance(last, ast.Constant) and isinstance(last.value, str):
                fallbacks[target.id] = last.value
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            fallbacks[target.id] = value.value
    return fallbacks


def ui_default() -> str:
    """Report the default frontend --ui resolves to.

    Read from the source rather than by running the CLI, so a test cannot change
    the environment it is checking.

    Returns:
        `"textual"` or `"ratatui"`; `"unknown"` when the definition moves.
    """
    tree = ast.parse(CLI_ARGS.read_text(encoding="utf-8"))
    fallbacks = _collect_fallbacks(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)):
            continue
        if node.args[0].value != "--ui":
            continue
        for keyword in node.keywords:
            if keyword.arg != "default":
                continue
            expression = keyword.value
            if isinstance(expression, ast.IfExp):
                return _resolve(expression.orelse, fallbacks)
            return _resolve(expression, fallbacks)
    return "unknown"


def test_the_default_ui_is_a_known_frontend() -> None:
    assert ui_default() in {"textual", "ratatui"}


def test_no_row_is_unstarted_once_ratatui_is_the_default() -> None:
    # The whole point of the ledger: the default may only move when every row is
    # ported, served server-side, or explicitly not UI behaviour.
    if ui_default() != "ratatui":
        return
    waiting = [
        f"{LEDGER.name}:{number} {cells[0]}"
        for number, cells in _rows()
        if _state(cells) == "not-started"
    ]
    assert not waiting, "ratatui is the default while these rows are not-started:\n" + "\n".join(
        waiting
    )


def test_every_ported_row_names_a_path_that_exists() -> None:
    missing: list[str] = []
    for number, cells in _rows():
        if _state(cells) != "ported":
            continue
        named = PATH_RE.findall(" | ".join(cells))
        if not named:
            missing.append(f"line {number}: {cells[0]} names no evidence path")
            continue
        missing.extend(
            f"line {number}: {cells[0]} names {path}, which does not exist"
            for path in named
            if not (ROOT / path).exists()
        )
    assert not missing, "\n".join(missing)


def test_every_ledger_row_names_a_file_that_exists() -> None:
    missing = [
        f"line {number}: {cells[0]}"
        for number, cells in _rows()
        if cells[0].startswith("`novacode_cli/tui/") and not (ROOT / cells[0].strip("`")).exists()
    ]
    assert not missing, "\n".join(missing)


def test_the_ledger_lists_every_tui_module() -> None:
    listed = {
        cells[0].strip("`") for _, cells in _rows() if cells[0].startswith("`novacode_cli/tui/")
    }
    present = {f"novacode_cli/tui/{path.name}" for path in TUI_DIR.glob("*.py")}
    assert present - listed == set(), "modules missing from the ledger"
    assert listed - present == set(), "ledger rows for deleted modules"


def test_every_state_is_one_the_ledger_defines() -> None:
    for number, cells in _rows():
        assert _state(cells) in STATES, f"line {number}: unknown state in {cells}"
