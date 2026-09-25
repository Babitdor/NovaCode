"""Handler for the /hooks command for hook management."""

import json

from rich.table import Table

from novacode_cli.config.config import COLORS, console
from novacode_cli.hooks import (
    HOOKS_DIR,
    HOOKS_FILE,
    HookEvent,
    _load_hooks,
    reload_hooks,
)


async def handle_hooks_command(cmd_args: str | None = None) -> bool:
    """Handle the /hooks command for hook management."""
    if cmd_args:
        args = cmd_args.strip().split()
        subcommand = args[0].lower() if args else None
        subargs = args[1:] if len(args) > 1 else []
    else:
        subcommand = None
        subargs = []

    if subcommand == "list":
        return _list_hooks()
    if subcommand in ("add", "remove", "enable", "disable", "test"):
        # Interactive subcommands removed with the console REPL — point at the TUI.
        console.print()
        console.print("[yellow]This command is interactive in the TUI.[/yellow]")
        console.print(
            "[dim]Use /hooks (the HooksScreen) instead — "
            "the console REPL was removed.[/dim]"
        )
        console.print()
        return True
    if subcommand == "reload":
        return _reload_hooks()
    if subcommand == "logs":
        return _view_logs(subargs)
    if subcommand == "events":
        return _list_events()
    if subcommand == "help":
        return _show_help()
    # Interactive menu removed with the console REPL — point at the TUI screen.
    console.print()
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /hooks (the HooksScreen) instead — "
        "the console REPL was removed.[/dim]"
    )
    console.print()
    return True


def _list_hooks() -> bool:
    """List all configured hooks."""
    hooks = _load_hooks()

    if not hooks:
        console.print()
        console.print("[yellow]No hooks configured[/yellow]")
        console.print()
        console.print("[dim]Use '/hooks add' to add a new hook[/dim]")
        console.print()
        return True

    table = Table(title="Configured Hooks", show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Command", style="cyan")
    table.add_column("Events", style="green")
    table.add_column("Status", style="yellow")

    for i, hook in enumerate(hooks, 1):
        command = " ".join(hook.get("command", []))
        events = ", ".join(hook.get("events", ["<all>"]))
        status = "✓" if hook.get("enabled", True) else "✗"
        table.add_row(str(i), command, events, status)

    console.print()
    console.print(table)
    console.print()
    console.print(f"[dim]{len(hooks)} hook(s) configured[/dim]")
    console.print()
    return True


def _reload_hooks() -> bool:
    """Reload hooks configuration from disk."""
    reload_hooks()
    console.print()
    console.print("[green]✓ Hooks configuration reloaded[/green]")
    console.print()
    return True


def _view_logs(args: list[str]) -> bool:
    """View hook logs."""
    log_dir = HOOKS_DIR / "logs"

    if not log_dir.exists():
        console.print()
        console.print("[yellow]No logs directory found[/yellow]")
        console.print("[dim]Logs will appear here after hooks are executed[/dim]")
        console.print()
        return True

    # Get log file
    log_file = log_dir / "hooks.log"

    if not log_file.exists():
        console.print()
        console.print("[yellow]No hook logs found[/yellow]")
        console.print("[dim]Logs will appear after hooks are executed[/dim]")
        console.print()
        return True

    # Read last N lines
    try:
        lines_count = int(args[0]) if args else 20
    except ValueError:
        lines_count = 20

    try:
        with open(log_file) as f:
            lines = f.readlines()
            last_lines = lines[-lines_count:]

        console.print()
        console.print(f"[bold]Last {lines_count} log entries:[/bold]")
        console.print()

        for line in last_lines:
            console.print(line.rstrip())

        console.print()
        console.print(f"[dim]{log_file}[/dim]")
        console.print()
    except Exception as e:
        console.print(f"[red]✗ Failed to read logs: {e}[/red]")

    return True


def _list_events() -> bool:
    """List all available hook events."""
    console.print()
    console.print("[bold]Available Hook Events[/bold]", style=COLORS["primary"])
    console.print()

    # Session events
    console.print("[bold]Session Events:[/bold]")
    console.print(f"  • {HookEvent.SESSION_START} - New session begins")
    console.print(f"  • {HookEvent.SESSION_END} - Session ends")
    console.print(f"  • {HookEvent.SESSION_SAVE} - Session saved")
    console.print(f"  • {HookEvent.SESSION_CONTINUE} - Session continued")
    console.print()

    # Model events
    console.print("[bold]Model Events:[/bold]")
    console.print(f"  • {HookEvent.MODEL_SWITCH} - Model switched")
    console.print()

    # Tool events
    console.print("[bold]Tool Events:[/bold]")
    console.print(f"  • {HookEvent.TOOL_CALL} - Tool invoked")
    console.print(f"  • {HookEvent.TOOL_RESULT} - Tool completed")
    console.print()

    # Message events
    console.print("[bold]Message Events:[/bold]")
    console.print(f"  • {HookEvent.AGENT_MESSAGE} - Agent sends message")
    console.print(f"  • {HookEvent.USER_MESSAGE} - User sends message")
    console.print()

    # Error events
    console.print("[bold]Error Events:[/bold]")
    console.print(f"  • {HookEvent.ERROR} - Error occurred")
    console.print()

    # Lifecycle events
    console.print("[bold]Lifecycle Events:[/bold]")
    console.print(f"  • {HookEvent.REMOTE_MESSAGE} - Remote (Discord/Telegram) message received")
    console.print(f"  • {HookEvent.CONTEXT_WARNING} - Context usage warning/critical")
    console.print(f"  • {HookEvent.COMPACT} - Conversation compacted")
    console.print(f"  • {HookEvent.INIT_COMPLETE} - /init pipeline completed")
    console.print()

    console.print("[dim]Leave 'events' empty to subscribe to all events[/dim]")
    console.print()

    return True


def _show_help() -> bool:
    """Show hooks command help."""
    console.print()
    console.print("[bold]Hooks Command Help[/bold]", style=COLORS["primary"])
    console.print()
    console.print("Usage: /hooks [command] [arguments]")
    console.print()
    console.print("[bold]Commands:[/bold]")
    console.print("  list              List all configured hooks")
    console.print("  add               Add a new hook interactively")
    console.print("  remove <#>        Remove hook by number")
    console.print("  enable <#>        Enable a hook")
    console.print("  disable <#>       Disable a hook")
    console.print("  test <#>          Test a hook with a test event")
    console.print("  reload            Reload hooks configuration")
    console.print("  logs [N]          View last N log entries (default: 20)")
    console.print("  events            List all available events")
    console.print("  help              Show this help message")
    console.print()
    console.print("[bold]Examples:[/bold]")
    console.print("  /hooks list                    # List all hooks")
    console.print("  /hooks add                     # Add a hook interactively")
    console.print("  /hooks remove 1                # Remove hook #1")
    console.print("  /hooks test 2                  # Test hook #2")
    console.print("  /hooks logs 50                 # View last 50 log entries")
    console.print()
    console.print(f"[dim]Configuration file: {HOOKS_FILE}[/dim]")
    console.print(f"[dim]Logs directory: {HOOKS_DIR / 'logs'}[/dim]")
    console.print()

    return True


def _save_hooks(hooks: list[dict]) -> bool:
    """Save hooks configuration to disk.
    
    Args:
        hooks: List of hook configurations
        
    Returns:
        True if saved successfully, False otherwise
    """
    try:
        # Ensure directory exists
        HOOKS_DIR.mkdir(parents=True, exist_ok=True)

        # Write configuration
        config = {"hooks": hooks}
        HOOKS_FILE.write_text(json.dumps(config, indent=2))

        # Reload hooks
        reload_hooks()

        return True
    except Exception as e:
        console.print(f"[red]✗ Failed to save hooks: {e}[/red]")
        return False


__all__ = ["handle_hooks_command"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_hooks_command(cmd_args=ctx.cmd_args)

    registry.register("hooks", _handle)
