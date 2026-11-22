"""Generate the server's slash-command catalogue from the TUI command table.

`novacode_cli/tui/app.py` holds the single `TUI_COMMANDS` table that drives the
Textual UI's autocomplete, dispatch and `/help`. The ratatui server needs the same
table, but it must not import Textual (the server is a headless process and the
wheel may not ship a UI toolkit). This script parses the table with :mod:`ast`
and writes `novacode_cli/server/catalog.py` as pure data.

Every row carries two facts an audit can check:

* `route` - where the behaviour lives: `passthrough` (the shared command layer in
  `novacode_cli/commands` runs it and returns text), `screen` (it needs a modal)
  or `client` (the connected frontend performs it);
* `served` - whether the socket port can answer that route *today*. A row that
  is not served answers `NOT_PORTED_MESSAGE` rather than pretending to have run,
  which is what keeps `rust/LEDGER.md`'s `not-started` rows honest.

`--check` regenerates in memory and compares, exiting non-zero on drift. That is
the drift guard: adding a command to the table without regenerating fails a test
instead of quietly leaving the command unreachable.

Usage:
    uv run python scripts/gen_tui_command_catalog.py [--check]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "novacode_cli" / "tui" / "app.py"
COMMANDS_DIR = ROOT / "novacode_cli" / "commands"
CATALOG_PATH = ROOT / "novacode_cli" / "server" / "catalog.py"
WORKLIST_PATH = ROOT / "rust" / "PARITY_COMMANDS.md"

TABLE_NAME = "TUI_COMMANDS"
APP_CLASS = "NovaApp"
# A SlashCommand literal always carries at least handler and help.
MIN_SLASH_ARGS = 2

# Commands the connected client performs itself: the server only names the
# action, because the effect (clearing a transcript, closing a window) belongs
# to whoever owns the screen.
CLIENT_ACTIONS: dict[str, str] = {
    "clear": "clear",
    "quit": "quit",
    "exit": "quit",
    "q": "quit",
}

# Commands the port answers without going through the shared command layer.
# `status` is composed from live session facts; `help` is special-cased inside
# `novacode_cli/commands/commands.py` before the registry. Both are pinned by
# tests/test_tui_command_port.py, which is the evidence for this set.
PORT_SPECIALS: frozenset[str] = frozenset({"status", "help"})

# Commands the port serves by building a screen itself, with the pure builders in
# novacode_cli/ui/screen_specs.py. Each is pinned by a test that drives the port
# and checks the spec, so this set cannot grow without evidence.
PORT_SCREENS: frozenset[str] = frozenset(
    {
        "command-center",
        "context",
        "oracle-mode",
        "resume",
        "rewind",
        "settings",
        "statusline",
        "subagents",
        "tabs",
        "theme",
        "tree",
        "update",
        "usage",
        "voice",
    }
)

# Commands the port answers itself with no screen: the behaviour lives on the
# session the socket client cannot reach, so the port composes the reply
# (see PORT_COMMANDS in novacode_cli/ui/port_server.py).
PORT_COMMANDS: frozenset[str] = frozenset(
    {
    "auto-approve",
    "artifacts",
    "share",
    "computer",
    "copy",
    "council",
    "agent-server",
    "btw",
    "ci-fix",
    "cost",
    "cowork",
    "cron",
    "export",
    "files",
    "goal",
    "images",
    "nova",
    "ponytail-audit",
    "ponytail-debt",
    "ponytail-gain",
    "ponytail-help",
    "ui",
    "import",
    "compare",
    "ralph",
    "trello",
    "create",
    "ponytail-review",
    "prime",
    "proactive",
    "prompt",
    "reindex",
    "redirect",
    "reload-plugins",
    "notifications",
    "research",
    "rooms",
    "steer",
    "stop",
    "tasks",
    "teammates",
    "tokens",
    "verbose",
    "webhook",
    "worktree",
}
)

# A handler that opens a browser, shells out or reads the OS keychain can block
# the command coroutine for seconds (Windows credential dialogs do not yield),
# so those commands stay unserved until the host runs commands as a cancellable
# task with progress frames - step 8 of the port. Detected from imports, not
# guessed: network, process and browser modules are the evidence.
BACKGROUND_IMPORTS: frozenset[str] = frozenset(
    {
        "http",
        "http.client",
        "httpx",
        "playwright",
        "requests",
        "selenium",
        "socket",
        "subprocess",
        "urllib",
        "webbrowser",
    }
)


@dataclass(frozen=True)
class Row:
    """One classified catalogue row."""

    name: str
    handler: str
    help: str
    route: str
    served: bool
    evidence: str
    wants_text: bool = True
    aliases: tuple[str, ...] = ()


def _call_names(node: ast.AST) -> set[str]:
    """Collect the name of every call inside *node*.

    Args:
        node: Function body to scan.

    Returns:
        Call names, with `self.x` reduced to `x` and `module.x` to `.x`.
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == "self":
                found.add(func.attr)
            else:
                found.add(f".{func.attr}")
        elif isinstance(func, ast.Name):
            found.add(func.id)
    return found


def _closure(calls: dict[str, set[str]], methods: set[str], name: str) -> set[str]:
    """Transitively expand *name* through calls to other app methods.

    Args:
        calls: Direct call names per method.
        methods: Names of the app's methods.
        name: Method to expand.

    Returns:
        Every call name reachable from *name*, including indirect ones.
    """
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for callee in calls.get(current, ()):
            seen.add(callee)
            if callee in methods and callee not in seen:
                pending.append(callee)
    return seen


def _table_node(tree: ast.Module) -> ast.Dict:
    """Find the `TUI_COMMANDS` dict literal in a parsed module.

    Args:
        tree: Parsed `novacode_cli/tui/app.py`.

    Returns:
        The dict literal node.

    Raises:
        SystemExit: When the table is missing or is not a dict literal.
    """
    for node in tree.body:
        target = getattr(node, "target", None)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(target, ast.Name)
            and target.id == TABLE_NAME
            and isinstance(node.value, ast.Dict)
        ):
            return node.value
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(isinstance(t, ast.Name) and t.id == TABLE_NAME for t in node.targets)
        ):
            return node.value
    sys.stderr.write(f"{TABLE_NAME} not found in {APP_PATH}\n")
    raise SystemExit(2)


def _app_methods(tree: ast.Module) -> tuple[dict[str, set[str]], set[str]]:
    """Collect the app's methods and their direct calls.

    Args:
        tree: Parsed `novacode_cli/tui/app.py`.

    Returns:
        A `(calls, method_names)` pair.
    """
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == APP_CLASS:
            calls = {
                member.name: _call_names(member)
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            return calls, set(calls)
    return {}, set()


def _slash_command(name: str, value: ast.AST) -> tuple[str, str, bool, tuple[str, ...]]:
    """Decode one `SlashCommand(...)` entry.

    Args:
        name: The dict key, used for diagnostics.
        value: The dict value node.

    Returns:
        `(handler, help, wants_text, aliases)`.

    Raises:
        SystemExit: When the entry is not a literal `SlashCommand` call.
    """
    if not isinstance(value, ast.Call) or len(value.args) < MIN_SLASH_ARGS:
        sys.stderr.write(f"{name}: entry is not a SlashCommand(handler, help) call\n")
        raise SystemExit(2)
    handler, help_text = value.args[0], value.args[1]
    if not isinstance(handler, ast.Constant) or not isinstance(help_text, ast.Constant):
        sys.stderr.write(f"{name}: handler and help must be string literals\n")
        raise SystemExit(2)
    keywords = {keyword.arg: keyword.value for keyword in value.keywords}
    wants = keywords.get("wants_text")
    wants_text = not (isinstance(wants, ast.Constant) and wants.value is False)
    aliases_node = keywords.get("aliases")
    aliases: tuple[str, ...] = ()
    if isinstance(aliases_node, ast.Tuple):
        aliases = tuple(
            element.value for element in aliases_node.elts if isinstance(element, ast.Constant)
        )
    return str(handler.value), str(help_text.value), wants_text, aliases


def _imports_any(tree: ast.Module, modules: frozenset[str]) -> bool:
    """Report whether a module imports any of *modules*.

    Args:
        tree: Parsed module.
        modules: Dotted module names to look for, matched on the leading parts.

    Returns:
        `True` when at least one import names one of *modules*.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == name or alias.name.startswith(f"{name}.") for name in modules):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            dotted = node.module
            if any(dotted == name or dotted.startswith(f"{name}.") for name in modules):
                return True
    return False


def _command_layer() -> tuple[dict[str, str], set[str], set[str]]:
    """Parse `novacode_cli/commands` for registry entries, modal and blocking modules.

    A command is reachable server-side when the shared layer registers it, its
    module never imports the Textual UI, and it cannot block the event loop. That
    is the audit step 8 of the port does by hand, done here mechanically so it
    cannot be skipped.

    Returns:
        `(registered, tui_importers, blocking)`: command name to module filename,
        the module filenames that still import `novacode_cli.tui`, and the
        filenames that import a network, process or browser module.
    """
    registered: dict[str, str] = {}
    tui_importers: set[str] = set()
    blocking: set[str] = set()
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            imported = node.names if isinstance(node, (ast.ImportFrom, ast.Import)) else ()
            imports_tui = (module or "").startswith("novacode_cli.tui") or any(
                alias.name.startswith("novacode_cli.tui") for alias in imported
            )
            if imports_tui:
                tui_importers.add(path.name)
        if _imports_any(tree, BACKGROUND_IMPORTS):
            blocking.add(path.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "register":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            value = node.args[0].value
            if isinstance(value, str):
                registered.setdefault(value, path.name)
    return registered, tui_importers, blocking


def build() -> list[Row]:
    """Classify every command in the TUI table.

    Returns:
        The catalogue rows in table order, which also drives `/help` order.
    """
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))
    table = _table_node(tree)
    calls, methods = _app_methods(tree)
    registered, tui_importers, blocking = _command_layer()
    rows: list[Row] = []
    for key, value in zip(table.keys, table.values, strict=True):
        if not isinstance(key, ast.Constant):
            continue
        name = str(key.value)
        handler, help_text, wants_text, aliases = _slash_command(name, value)
        reach = _closure(calls, methods, handler)
        served_by_layer = name in registered and registered[name] not in tui_importers
        background = name in registered and registered[name] in blocking
        if name in CLIENT_ACTIONS:
            route, served, evidence = "client", True, "client-action"
        elif name in PORT_SCREENS:
            route, served, evidence = "screen", True, "port-screen"
        elif name in PORT_COMMANDS:
            route, served, evidence = "passthrough", True, "port-command"
        elif name in PORT_SPECIALS or (served_by_layer and not background):
            route, served, evidence = "passthrough", True, "shared-command"
        elif "push_screen" in reach or "push_screen_wait" in reach:
            route, served, evidence = "screen", False, "modal"
        elif background:
            route, served, evidence = "passthrough", False, "background"
        else:
            route, served, evidence = "passthrough", False, "app-side"
        rows.append(
            Row(
                name=name,
                handler=handler,
                help=help_text,
                route=route,
                served=served,
                evidence=evidence,
                wants_text=wants_text,
                aliases=aliases,
            )
        )
    return rows


HEADER = r'''"""Catalogue of every slash command the TUI exposes. GENERATED - do not edit.

Produced by `scripts/gen_tui_command_catalog.py` from the `TUI_COMMANDS` table in
`novacode_cli/tui/app.py` plus the registry in `novacode_cli/commands`. Regenerate
with::

    uv run python scripts/gen_tui_command_catalog.py

`--check` exits non-zero when this file and those sources disagree, which is what
keeps the command surface from drifting: a command added to the table without a
regenerate fails a test instead of becoming silently unreachable.

Textual is never imported: the table is parsed with `ast` at generation time and
this module is plain data, so the server runs on a machine with no UI toolkit.
"""

from __future__ import annotations

from dataclasses import dataclass

# Where a command's behaviour lives; see `CommandSpec.route`.
ROUTE_PASSTHROUGH = "passthrough"
ROUTE_SCREEN = "screen"
ROUTE_CLIENT = "client"

ROUTES: tuple[str, ...] = (ROUTE_PASSTHROUGH, ROUTE_SCREEN, ROUTE_CLIENT)

# How a row earned its route, so an audit checks evidence instead of a hunch.
EVIDENCE_CLIENT_ACTION = "client-action"
EVIDENCE_SHARED_COMMAND = "shared-command"
EVIDENCE_MODAL = "modal"
EVIDENCE_APP_SIDE = "app-side"
EVIDENCE_BACKGROUND = "background"
EVIDENCE_PORT_SCREEN = "port-screen"
EVIDENCE_PORT_COMMAND = "port-command"

EVIDENCES: tuple[str, ...] = (
    EVIDENCE_CLIENT_ACTION,
    EVIDENCE_SHARED_COMMAND,
    EVIDENCE_MODAL,
    EVIDENCE_APP_SIDE,
    EVIDENCE_BACKGROUND,
    EVIDENCE_PORT_SCREEN,
    EVIDENCE_PORT_COMMAND,
)


@dataclass(frozen=True)
class CommandSpec:
    """One slash command, as the server needs to know it.

    Attributes:
        name: Command as typed, without the leading slash.
        handler: The Textual method that owns the behaviour, for cross-reference.
        help: One-line help text, verbatim from the TUI table.
        route: Where the behaviour lives: `ROUTE_PASSTHROUGH`, `ROUTE_SCREEN` or
            `ROUTE_CLIENT`.
        served: Whether the socket port answers this command today. A `False` row
            answers `NOT_PORTED_MESSAGE` rather than doing nothing.
        evidence: How the route was decided, one of `EVIDENCES`.
        wants_text: Whether the handler consumes the argument text.
        aliases: Alternative spellings declared in the table.
    """

    name: str
    handler: str
    help: str
    route: str
    served: bool
    evidence: str
    wants_text: bool = True
    aliases: tuple[str, ...] = ()


CATALOG: dict[str, CommandSpec] = {
'''

FOOTER = r'''}

# Alias to canonical name, so `/exit` and `/q` behave like `/quit`.
ALIASES: dict[str, str] = {
`aliases`}


def get(name: str) -> CommandSpec | None:
    """Look one command up, resolving aliases.

    Args:
        name: Command name with or without a leading slash, in any case.

    Returns:
        The matching spec, or `None` when the command does not exist.
    """
    cleaned = name.strip().lstrip("/").lower().split(maxsplit=1)[0] if name.strip() else ""
    return CATALOG.get(cleaned) or CATALOG.get(ALIASES.get(cleaned, ""))


def route_of(name: str) -> str | None:
    """Report where a command's behaviour lives.

    Args:
        name: Command name with or without a leading slash.

    Returns:
        The route, or `None` for an unknown command.
    """
    spec = get(name)
    return spec.route if spec else None


def served_by(name: str) -> bool:
    """Report whether the socket port can answer a command today.

    Args:
        name: Command name with or without a leading slash.

    Returns:
        `True` when the port serves the command, else `False`.
    """
    spec = get(name)
    return bool(spec and spec.served)


def names() -> tuple[str, ...]:
    """List every canonical command name, in table order.

    Returns:
        The command names, without aliases.
    """
    return tuple(CATALOG)
'''


def _aliases(rows: list[Row]) -> dict[str, str]:
    """Build the alias to canonical name map.

    Args:
        rows: Catalogue rows.

    Returns:
        Alias to canonical command name, including the client's short forms.
    """
    aliases: dict[str, str] = {}
    for row in rows:
        for alias in row.aliases:
            aliases[alias] = row.name
    for alias, canonical in CLIENT_ACTIONS.items():
        if alias != canonical:
            aliases.setdefault(alias, canonical)
    return aliases


def _literal(value: str) -> str:
    """Render a string literal the way ruff's linter wants it.

    Args:
        value: Text to render.

    Returns:
        A double-quoted Python literal with non-ASCII kept readable.
    """
    return json.dumps(value, ensure_ascii=False)


def _tuple_literal(values: tuple[str, ...]) -> str:
    """Render a tuple literal the way ruff's formatter would.

    Args:
        values: Tuple contents.

    Returns:
        A Python tuple literal.
    """
    if not values:
        return "()"
    if len(values) == 1:
        return f"({_literal(values[0])},)"
    return "(" + ", ".join(_literal(value) for value in values) + ")"


def render(rows: list[Row]) -> str:
    """Render the catalogue module.

    Args:
        rows: Classified rows, in table order.

    Returns:
        The module source, newline-terminated.
    """
    lines = [HEADER]
    for row in rows:
        lines.append(f"    {_literal(row.name)}: CommandSpec(\n")
        lines.append(f"        name={_literal(row.name)},\n")
        lines.append(f"        handler={_literal(row.handler)},\n")
        lines.append(f"        help={_literal(row.help)},\n")
        lines.append(f"        route=ROUTE_{row.route.upper()},\n")
        lines.append(f"        served={row.served},\n")
        lines.append(f"        evidence=EVIDENCE_{row.evidence.upper().replace('-', '_')},\n")
        lines.append(f"        wants_text={row.wants_text},\n")
        lines.append(f"        aliases={_tuple_literal(row.aliases)},\n")
        lines.append("    ),\n")
    aliases = "".join(
        f"    {_literal(alias)}: {_literal(name)},\n" for alias, name in _aliases(rows).items()
    )
    return "".join(lines) + FOOTER.replace("`aliases`", aliases)


WORKLIST_HEADER = """# Command parity worklist - Textual TUI_COMMANDS to ratatui

GENERATED by scripts/gen_tui_command_catalog.py. Do not edit by hand:
regenerate with `uv run python scripts/gen_tui_command_catalog.py`, which also
rewrites novacode_cli/server/catalog.py from the same parse.

One row per command, in the order the TUI /help prints them. Route is where
the behaviour has to live, Served is whether the socket port answers it today,
and Evidence is how that was decided:

| Evidence | Meaning |
|---|---|
| shared-command | Registered in the layer, no Textual import, cannot block: the port runs it. |
| modal | The handler pushes a screen; it needs a spec plus the client screen renderer. |
| app-side | The handler only exists inside NovaApp; re-point it at UIPort first. |
| background | Opens a browser, a subprocess or the keychain; needs a cancellable host task. |
| client-action | The frontend performs it (clear, quit); the server names the action. |

"""


def render_worklist(rows: list[Row]) -> str:
    """Render the parity worklist for every command.

    Args:
        rows: Classified rows, in table order.

    Returns:
        The Markdown document, newline-terminated.
    """
    served = [row for row in rows if row.served]
    waiting = [row for row in rows if not row.served]
    counts: dict[str, int] = {}
    for row in waiting:
        counts[row.evidence] = counts.get(row.evidence, 0) + 1
    lines = [WORKLIST_HEADER]
    lines.append("## Summary\n\n")
    lines.append(f"* {len(rows)} commands in the TUI table.\n")
    lines.append(f"* **{len(served)} served** by the socket port today.\n")
    detail = ", ".join(f"{name}: {count}" for name, count in sorted(counts.items()))
    lines.append(f"* **{len(waiting)} waiting**, by evidence - {detail}.\n")
    lines.append("\n## Worklist\n\n")
    lines.append("| Command | Route | Served | Evidence | Textual handler | Aliases |\n")
    lines.append("|---|---|---|---|---|---|\n")
    for row in rows:
        aliases = ", ".join(row.aliases) if row.aliases else "-"
        mark = "yes" if row.served else "**no**"
        lines.append(
            f"| /{row.name} | {row.route} | {mark} | {row.evidence} | {row.handler} | {aliases} |\n"
        )
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Write the catalogue and the worklist, or check both for drift.

    Args:
        argv: Command-line arguments; `--check` compares only.

    Returns:
        Process exit code: `0` on success, `1` on drift.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    args = parser.parse_args(argv)
    rows = build()
    outputs = (
        (CATALOG_PATH, render(rows)),
        (WORKLIST_PATH, render_worklist(rows)),
    )
    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, rendered in outputs
            if (path.read_text(encoding="utf-8") if path.exists() else "") != rendered
        ]
        if stale:
            for name in stale:
                sys.stderr.write(f"{name} is stale\n")
            sys.stderr.write("rerun scripts/gen_tui_command_catalog.py\n")
            return 1
        sys.stdout.write(f"catalog ok: {len(rows)} commands\n")
        return 0
    for path, rendered in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        sys.stdout.write(f"wrote {path.relative_to(ROOT)}\n")
    sys.stdout.write(f"{len(rows)} commands\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
