"""Handler for the /model command for LLM provider management."""

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import console


async def handle_model_command(ctx: CommandContext) -> bool:
    """Handle the /model command — interactive management lives in the TUI."""
    console.print()
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /model to open the model picker (ModelScreen) — "
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
        return await handle_model_command(ctx)

    registry.register("model", _handle)
