"""Handler for the /mcp command for MCP server management."""

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import console


async def handle_mcp_command(ctx: CommandContext) -> bool:
    """Handle the /mcp command — interactive management lives in the TUI."""
    console.print()
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /mcp to open the MCP servers screen (McpScreen) — "
        "the console REPL was removed.[/dim]"
    )
    console.print()
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_mcp_command(ctx)

    registry.register("mcp", _handle)
