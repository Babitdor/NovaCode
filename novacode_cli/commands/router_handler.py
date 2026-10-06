"""Handler for the /router command — per-turn model routing.

The real configuration lives in the TUI (``RouterScreen``); this is the console
stub, matching ``model_handler``. It exists so ``/router`` is a known command on
every path that dispatches through the registry, rather than an unknown-command
error on the non-interactive ones.
"""

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import console


async def handle_router_command(ctx: CommandContext) -> bool:
    """Handle the /router command — interactive management lives in the TUI."""
    console.print()
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /router to configure per-turn model routing (RouterScreen) — "
        "the console REPL was removed.[/dim]"
    )
    console.print()
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    """Register the /router handler with the command registry."""
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_router_command(ctx)

    registry.register("router", _handle)
