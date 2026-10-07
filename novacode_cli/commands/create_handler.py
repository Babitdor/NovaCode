"""Handler for the /create command — Skills & Agents web UI.

Usage:
    /create              - Start the Skills & Agents web UI and open browser
    /create stop         - Stop the web UI server

The web UI is a self-contained HTML page served from a local HTTP server.
Users can browse, preview, edit, create, and delete skills (SKILL.md) and
agents (agent.md) across global and project scopes.
"""

from __future__ import annotations

import webbrowser
from typing import Any

from novacode_cli.config.config import console


async def handle_create_command(
    session_state: Any,
    cmd_args: str | None = None,
) -> bool:
    """Handle the /create command.

    Args:
        session_state: Current session state.
        cmd_args: Command arguments (e.g., "stop").

    Returns:
        True if the command was handled.
    """
    from novacode_cli.commands.create_server import CreateServer

    # Handle subcommands
    if cmd_args and cmd_args.strip() == "stop":
        return _handle_stop(session_state)

    # Check if already running
    existing_server: CreateServer | None = getattr(
        session_state, "create_server", None
    )
    if existing_server and existing_server.is_running:
        console.print()
        console.print(
            f"[yellow]Create UI is already running at "
            f"[cyan]{existing_server.url}[/cyan][/yellow]"
        )
        console.print("[dim]Use /create stop to stop it.[/dim]")
        console.print()
        return True

    # Start the server
    server = CreateServer()
    await server.start()

    # Store on session_state for lifecycle management
    session_state.create_server = server

    # Open browser
    try:
        webbrowser.open(f"{server.url}")
    except Exception:
        pass  # Browser opening is best-effort

    console.print()
    console.print(
        f"[green]✓[/green] Create UI started at "
        f"[cyan]{server.url}[/cyan]"
    )
    console.print(
        "[dim]Browse, preview, edit, and create skills & agents in the browser.[/dim]"
    )
    console.print("[dim]Use /create stop to shut down the server.[/dim]")
    console.print()

    return True


def _handle_stop(session_state: Any) -> bool:
    """Stop the Create UI server.

    Args:
        session_state: Current session state.

    Returns:
        True if the command was handled.
    """
    from novacode_cli.commands.create_server import CreateServer

    server: CreateServer | None = getattr(session_state, "create_server", None)
    if server and server.is_running:
        server.stop()
        session_state.create_server = None
        console.print()
        console.print("[green]✓[/green] Create UI stopped.")
        console.print()
    else:
        console.print()
        console.print("[yellow]Create UI is not running.[/yellow]")
        console.print()

    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_commands(registry) -> None:
    """Register the /create command."""
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_create_command(
            session_state=ctx.session_state,
            cmd_args=ctx.cmd_args,
        )

    registry.register("create", _handle)
