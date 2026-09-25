"""Handlers for server-related commands: /servers, /tests, /kill."""

from pathlib import Path

from rich.table import Table

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import COLORS, console
from novacode_cli.process_manager import ProcessManager
from novacode_cli.server_runner.dev_server import list_servers
from novacode_cli.server_runner.test_runner import (
    detect_test_framework,
    get_default_test_command,
    run_tests,
)


async def handle_servers_command(ctx: CommandContext) -> bool:
    """Handle /servers — show the running servers table, then a TUI pointer."""

    servers = list_servers(include_external=True)

    if not servers:
        console.print()
        console.print("[yellow]No dev servers running[/yellow]")
        console.print("[dim]Use the start_dev_server tool to start a server[/dim]")
        console.print()
        return True

    # Display servers in a table
    table = Table(show_header=True, header_style="bold")
    table.add_column("PID", style="dim")
    table.add_column("Name")
    table.add_column("URL")
    table.add_column("Status")
    table.add_column("Command", style="dim")

    managed_servers = []
    external_servers = []
    for server in servers:
        if server.pid == 0 and "external" in server.name:
            external_servers.append(server)
        else:
            managed_servers.append(server)

    for server in managed_servers:
        status_style = "green" if server.status.value == "healthy" else "yellow"
        table.add_row(
            str(server.pid),
            server.name,
            server.url,
            f"[{status_style}]{server.status.value}[/{status_style}]",
            server.command[:40] + "..." if len(server.command) > 40 else server.command,
        )

    for server in external_servers:
        status_style = "green" if server.status.value == "healthy" else "yellow"
        table.add_row(
            "[dim]external[/dim]",
            f"[dim]{server.name}[/dim]",
            server.url,
            f"[{status_style}]{server.status.value}[/{status_style}]",
            "[dim](not managed by CLI)[/dim]",
        )

    console.print()
    console.print(table)
    if external_servers:
        console.print("[dim]Note: External servers (marked 'external') were started outside this CLI and cannot be stopped here.[/dim]")
    console.print()

    # Interactive menu removed with the console REPL — point at the TUI screen.
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /servers (the ServersScreen) instead — "
        "the console REPL was removed.[/dim]"
    )
    console.print()
    return True


async def handle_tests_command(session_state, cmd_args: str | None = None) -> bool:
    """Handle /tests - run project tests.

    Args:
        session_state: Current session state
        cmd_args: Optional test command arguments

    Returns:
        True (command always handled)
    """
    console.print()
    console.print("[bold]Running Tests[/bold]", style=COLORS["primary"])
    console.print()

    working_dir = str(Path.cwd())

    # Detect framework if no command specified
    if not cmd_args:
        framework = detect_test_framework(working_dir)
        command = get_default_test_command(framework)

        if not command:
            console.print("[yellow]Could not auto-detect test framework[/yellow]")
            console.print(
                "[dim]Specify a command: /tests pytest or /tests npm test[/dim]"
            )
            console.print()
            return True

        console.print(f"[dim]Detected framework: {framework.value}[/dim]")
        console.print(f"[dim]Running: {command}[/dim]")
    else:
        command = cmd_args.strip()
        console.print(f"[dim]Running: {command}[/dim]")

    console.print()

    # Stream output callback
    def output_callback(line: str) -> None:
        console.print(f"[dim]{line}[/dim]", markup=False)

    # Run tests with streaming output
    result = await run_tests(
        command=command,
        working_dir=working_dir,
        output_callback=output_callback,
    )

    console.print()

    # Show summary
    if result.success:
        console.print("[green]✓ Tests passed![/green]")
    else:
        console.print("[red]✗ Tests failed[/red]")

    # Show statistics if available
    stats_parts = []
    if result.tests_run is not None:
        stats_parts.append(f"{result.tests_run} tests")
    if result.tests_passed is not None:
        stats_parts.append(f"{result.tests_passed} passed")
    if result.tests_failed is not None:
        stats_parts.append(f"{result.tests_failed} failed")
    if result.duration_seconds is not None:
        stats_parts.append(f"{result.duration_seconds:.2f}s")

    if stats_parts:
        console.print(f"[dim]{', '.join(stats_parts)}[/dim]")

    if result.error:
        console.print(f"[red]Error: {result.error}[/red]")

    # Record a notification summarizing the run.
    try:
        passed = result.tests_passed
        total = result.tests_run
        title = (
            f"Tests: {passed}/{total} passed"
            if passed is not None and total
            else ("Tests passed" if result.success else "Tests failed")
        )
        msg = f"{result.tests_failed} failed" if result.tests_failed is not None else ""
        if result.duration_seconds is not None:
            msg = (msg + " · " if msg else "") + f"{result.duration_seconds:.1f}s"
        session_state.add_notification(
            level="success" if result.success else "error",
            title=title,
            message=msg or ("ok" if result.success else "failed"),
            source="tests",
        )
    except Exception:  # noqa: BLE001
        pass

    console.print()
    return True


async def handle_kill_command(session_state, cmd_args: str | None = None) -> bool:
    """Handle /kill - kill a running process by PID or name.

    Args:
        session_state: Current session state
        cmd_args: Optional PID or name to kill

    Returns:
        True (command always handled)
    """
    manager = ProcessManager.get_instance()

    console.print()

    # If argument provided, try to kill directly
    if cmd_args:
        arg = cmd_args.strip()

        # Try as PID first
        try:
            pid = int(arg)
            result = await manager.stop_process(pid)
            if result:
                console.print(f"[green]✓ Killed process {pid}[/green]")
            else:
                console.print(f"[yellow]No process found with PID {pid}[/yellow]")
            console.print()
            return True
        except ValueError:
            pass

        # Try as name
        result = await manager.stop_by_name(arg)
        if result:
            console.print(f"[green]✓ Killed process '{arg}'[/green]")
        else:
            console.print(f"[yellow]No process found with name '{arg}'[/yellow]")
        console.print()
        return True

    # No argument - interactive picker removed with the console REPL — point at the TUI.
    console.print("[yellow]This command is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /kill (the TUI's process picker) instead — "
        "the console REPL was removed.[/dim]"
    )
    console.print("[dim]Or pass a PID/name: /kill <pid|name>[/dim]")
    console.print()
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle_servers(ctx: CommandContext) -> bool:
        return await handle_servers_command(ctx)

    async def _handle_tests(ctx: CommandContext) -> bool:
        return await handle_tests_command(ctx.session_state, cmd_args=ctx.cmd_args)

    async def _handle_kill(ctx: CommandContext) -> bool:
        return await handle_kill_command(ctx.session_state, cmd_args=ctx.cmd_args)

    registry.register("servers", _handle_servers)
    registry.register("tests", _handle_tests)
    registry.register("kill", _handle_kill)
