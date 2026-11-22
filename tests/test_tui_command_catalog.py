"""The generated command catalogue: coverage, routes and drift.

The ratatui client can only offer what the server tells it about, and the server
only knows what `novacode_cli/server/catalog.py` says. These tests pin the three
ways that file can lie:

* it can miss a command (someone added a row to `TUI_COMMANDS` and did not
  regenerate) - caught by comparing against the parsed table;
* it can claim a route the port does not honour - caught by checking every row
  against `ROUTES` and by driving the port over each served route;
* it can drag Textual into the server - caught by importing the catalogue in a
  clean interpreter and asserting the UI toolkit never loaded.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from novacode_cli.server.catalog import (
    ALIASES,
    CATALOG,
    EVIDENCES,
    ROUTES,
    get,
    names,
    route_of,
    served_by,
)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "novacode_cli" / "tui" / "app.py"
GENERATOR = ROOT / "scripts" / "gen_tui_command_catalog.py"


def _table_names() -> list[str]:
    """Read `TUI_COMMANDS` names straight from app.py, without importing it.

    Returns:
        Command names in table order.
    """
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(target, ast.Name)
            and target.id == "TUI_COMMANDS"
            and isinstance(node.value, ast.Dict)
        ):
            return [key.value for key in node.value.keys if isinstance(key, ast.Constant)]
    msg = "TUI_COMMANDS not found"
    raise AssertionError(msg)


def test_catalog_lists_every_tui_command_in_table_order() -> None:
    expected = _table_names()
    assert len(expected) == 105
    assert list(CATALOG) == expected


def test_names_returns_the_canonical_names() -> None:
    assert names() == tuple(CATALOG)


def test_catalog_is_not_stale() -> None:
    # Current interpreter, fixed repository path, no shell.
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "catalog ok" in result.stdout


def test_every_route_and_evidence_is_declared() -> None:
    for spec in CATALOG.values():
        assert spec.route in ROUTES, spec
        assert spec.evidence in EVIDENCES, spec
        # Every row names a NovaApp method: the `_run_*` family,
        # `_passthrough_command` for the print-only commands, or `action_quit`.
        assert spec.handler.startswith(("_", "action")), spec


def test_unserved_rows_say_why_they_wait() -> None:
    waiting = [spec for spec in CATALOG.values() if not spec.served]
    assert waiting, "a catalogue with everything served would hide the work left"
    for spec in waiting:
        assert spec.evidence in {"modal", "app-side", "background"}, spec


def test_served_rows_are_passthrough_client_or_a_port_screen() -> None:
    # A served row that needs a modal must have a builder behind it, which is
    # what EVIDENCE_PORT_SCREEN records; anything else would be a row the port
    # claims to answer and cannot.
    for spec in CATALOG.values():
        if not spec.served:
            continue
        if spec.route == "screen":
            assert spec.evidence == "port-screen", spec
        else:
            assert spec.route in {"passthrough", "client"}, spec


def test_a_port_screen_row_is_reachable() -> None:
    spec = get("context")
    assert spec is not None
    assert (spec.route, spec.served, spec.evidence) == ("screen", True, "port-screen")


def test_client_actions_are_served() -> None:
    for command in ("clear", "quit", "exit"):
        assert served_by(command) is True
        assert route_of(command) == "client"


def test_aliases_resolve_to_their_canonical_command() -> None:
    assert get("?").name == "help"
    assert get("/models").name == "model"
    assert route_of("exit") == "client"
    assert route_of("cc") == get("command-center").route
    assert set(ALIASES.values()) <= set(CATALOG)


def test_unknown_commands_have_no_route() -> None:
    assert get("definitely-not-a-command") is None
    assert route_of("definitely-not-a-command") is None
    assert served_by("definitely-not-a-command") is False


@pytest.mark.parametrize("name", ["claude-plan", "remote", "loop"])
def test_unlanded_waves_are_explicitly_not_served(name: str) -> None:
    assert served_by(name) is False


def test_settings_is_served_by_a_port_screen() -> None:
    # The AGENT section travels as a form; the other sections stay Textual-only.
    assert (route_of("settings"), served_by("settings")) == ("screen", True)
    assert get("settings").evidence == "port-screen"


def test_theme_is_served_by_a_port_screen() -> None:
    # @@/theme@@ lists the generated table and answers with the chosen name.
    assert (route_of("theme"), served_by("theme")) == ("screen", True)
    assert get("theme").evidence == "port-screen"


@pytest.mark.parametrize("name", ["trello", "create", "ralph"])
def test_blocking_handlers_are_served_by_an_owned_task(name: str) -> None:
    # These open browsers or loop forever, so the port starts a task it owns and
    # answers immediately; the command coroutine can never wedge on them.
    spec = get(name)
    assert spec is not None
    assert spec.served is True, spec
    assert spec.route == "passthrough", spec


def test_the_parity_worklist_lists_every_command() -> None:
    # The worklist is the audit artefact for what is left: one row per command,
    # generated from the same parse as the catalogue. --check keeps it fresh.
    text = (ROOT / "rust" / "PARITY_COMMANDS.md").read_text(encoding="utf-8")
    assert "GENERATED by scripts/gen_tui_command_catalog.py" in text
    for name in CATALOG:
        assert f"| /{name} |" in text, f"{name} is missing from the worklist"
    served = sum(1 for spec in CATALOG.values() if spec.served)
    assert f"**{served} served**" in text
    assert f"**{len(CATALOG) - served} waiting**" in text


def test_importing_the_catalog_does_not_load_textual() -> None:
    code = (
        "import sys, novacode_cli.server.catalog as c; "
        "print(len(c.CATALOG), 'textual' in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603 - current interpreter, literal snippet
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["105", "False"]
