"""Handler for the /auth command (provider and service API keys).

Like /model, this is interactive only in the TUI: the console path prints a
pointer rather than prompting, because there is no REPL to prompt with.
"""

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import console


async def handle_auth_command(ctx: CommandContext) -> bool:
    """Handle the /auth command — interactive management lives in the TUI."""
    console.print()
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /auth to manage provider and service API keys "
        "(AuthManagerScreen) — the console REPL was removed.[/dim]"
    )
    console.print(
        "[dim]Keys are stored in your OS credential store and listed by `nova doctor`.[/dim]"
    )
    console.print()
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_auth_command(ctx)

    registry.register("auth", _handle)
    # `/connect` is the reference implementation's alias; keep it working so a
    # habit carried over from there lands on the same manager.
    registry.register("connect", _handle)
