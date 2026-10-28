"""Standard plugin management through Nova's shared command service."""

from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING

from novacode_cli.config.config import console
from novacode_cli.plugins.cli import execute

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry


def command_arguments(text: str) -> list[str]:
    """Keep quoted Windows paths intact without treating backslashes as escapes."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


async def handle_plugin_install_command(ctx: CommandContext) -> bool:
    """Run the same management service used by `nova plugin`."""
    try:
        result = await asyncio.to_thread(execute, command_arguments(ctx.cmd_args or "list"))
        for line in result.lines:
            console.print(line, markup=False)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        console.print(f"Plugin operation failed: {exc}", markup=False)
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Keep the plural spelling as an alias for the standard /plugin command."""
    registry.register("plugin", handle_plugin_install_command)
    registry.register("plugins", handle_plugin_install_command)
