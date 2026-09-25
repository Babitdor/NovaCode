"""Handler for the /middleware command for Nova plugin management.

Supports listing installed plugins, enabling/disabling them, and showing
details about what each plugin provides.
"""

from __future__ import annotations

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import console


async def handle_plugins_command(ctx: CommandContext) -> bool:
    """Handle /middleware — interactive management lives in the TUI."""
    console.print()
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /middleware to open the PluginsScreen — "
        "the console REPL was removed.[/dim]"
    )
    console.print()
    return True


def register_commands(registry):
    """Register /middleware (manages pip entry-point middleware/tool plugins).

    Formerly /plugins; that name now belongs to the Claude-compatible plugin
    installer (plugin_install_handler).
    """

    async def _middleware_handler(ctx: CommandContext) -> str | bool:
        return await handle_plugins_command(ctx)

    registry.register("middleware", _middleware_handler)
