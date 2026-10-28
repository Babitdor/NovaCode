"""One plugin management command service for the shell and Nova interfaces."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from novacode_cli.plugins import claude_plugins as cp
from novacode_cli.plugins import marketplaces as mp
from novacode_cli.plugins.components import Plugin


class _HelpRequestedError(Exception):
    """Carry formatted help to the interface instead of printing into the TUI."""


class Parser(argparse.ArgumentParser):
    """Report argument errors to the host without terminating the TUI."""

    def error(self, message: str) -> NoReturn:
        """Return parser errors to the calling interface."""
        raise ValueError(message)

    def print_help(self, file: object = None) -> None:  # noqa: ARG002 -- argparse interface.
        """Return help through the management result, including subcommand help."""
        raise _HelpRequestedError(self.format_help())


@dataclass(frozen=True)
class Result:
    """The UI chooses a report view or a temporary operation notification."""

    lines: tuple[str, ...]
    changed: bool = False
    report: bool = False


def parser() -> Parser:
    """Build the shared shell/slash-command grammar."""
    root = Parser(prog="nova plugin", description="Manage standard directory plugins")
    commands = root.add_subparsers(dest="action")
    for action in ("install", "uninstall", "remove", "enable", "disable", "update"):
        command = commands.add_parser(action)
        command.add_argument("plugin")
        command.add_argument("--scope", "-s", choices=("user", "project", "local"))
    for action in ("list", "search"):
        command = commands.add_parser(action)
        command.add_argument("query", nargs="?", default="")
    validate = commands.add_parser("validate")
    validate.add_argument("path")
    marketplace = commands.add_parser("marketplace")
    marketplace.add_argument("operation", choices=("add", "remove", "list", "update"))
    marketplace.add_argument("source", nargs="?", default="")
    return root


def execute(arguments: list[str]) -> Result:  # noqa: PLR0911, PLR0912 -- one branch per owner command.
    """Execute owner-requested management; install never runs plugin components."""
    try:
        args = parser().parse_args(arguments)
    except _HelpRequestedError as help_requested:
        return Result((str(help_requested),), report=True)
    action = args.action or "list"
    if action == "install":
        source = args.plugin
        if ("@" in source and not source.startswith(("git@", "ssh://"))) or (
            not Path(source).expanduser().is_dir() and "/" not in source and ":" not in source
        ):
            name = mp.install_plugin(source, scope=args.scope or "user")
        else:
            name = cp.install(source, scope=args.scope or "user")
        warnings = cp.plugin_components(name).get("diagnostics", [])
        return Result(
            (f"Installed {name}. /reload-plugins applies it to this session.", *warnings),
            changed=True,
        )
    if action in {"uninstall", "remove"}:
        removed = cp.remove(args.plugin, scope=args.scope or "user")
        return Result(
            (
                f"Uninstalled {args.plugin}."
                if removed
                else "No matching installation at that scope.",
            ),
            changed=removed,
        )
    if action in {"enable", "disable"}:
        cp.set_enabled(args.plugin, enabled=action == "enable", scope=args.scope)
        return Result(
            (f"{action.title()}d {args.plugin}. /reload-plugins applies this change.",),
            changed=True,
        )
    if action == "update":
        name = cp.update(args.plugin, scope=args.scope)
        return Result((f"Updated {name}. /reload-plugins loads the new snapshot.",), changed=True)
    if action == "validate":
        directory = Path(args.path).expanduser().resolve()
        if (directory / ".claude-plugin/marketplace.json").is_file():
            mp._marketplace_json(directory)
            warnings = []
        else:
            warnings = Plugin(directory, directory.name).validate()
        return Result(
            (
                "Validation passed."
                if not warnings
                else "Validation passed with compatibility warnings.",
                *warnings,
            ),
            report=True,
        )
    if action == "marketplace":
        operation, source = args.operation, args.source
        if operation == "list":
            return Result(
                tuple(f"{m['name']}: {m['source']}" for m in mp.list_marketplaces())
                or ("No marketplaces.",),
                report=True,
            )
        if not source:
            msg = "Marketplace operation requires a source or name"
            raise ValueError(msg)
        if operation == "add":
            name = mp.add(source)
            return Result((f"Added marketplace {name}.",), changed=True)
        if operation == "remove":
            removed = mp.remove_marketplace(source)
            return Result(
                ("Marketplace removed." if removed else "Marketplace not found.",), changed=removed
            )
        name = mp.update_marketplace(source)
        return Result((f"Updated marketplace {name}.",), changed=True)
    if action == "search":
        query = args.query.lower()
        matches = [
            p
            for p in mp.list_marketplace_plugins()
            if query in (p["name"] + " " + p["description"]).lower()
        ]
        return Result(
            tuple(f"{p['name']}@{p['marketplace']}: {p['description']}" for p in matches)
            or ("No matching plugins.",),
            report=True,
        )
    return Result(
        tuple(
            f"{p['id']} ({p['scope']}, {'enabled' if p['enabled'] else 'disabled'}) "
            f"{p.get('version', 'unversioned')}"
            for p in cp.list_plugins()
        )
        or ("No plugins installed.",),
        report=True,
    )


def main(arguments: list[str]) -> int:
    """Run without an agent or provider; use the same ownership and scope rules."""
    try:
        result = execute(arguments)
        for line in result.lines:
            sys.stdout.write(line + "\n")
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        sys.stderr.write(f"Plugin operation failed: {exc}\n")
        return 1
    return 0
