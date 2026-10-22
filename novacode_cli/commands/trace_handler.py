"""Handler for the /trace command for LangSmith tracing management."""

import os

from rich.panel import Panel
from rich.text import Text

from novacode_cli.config.config import COLORS, console


async def handle_trace_command(cmd_args: list[str]) -> bool:
    """Handle /trace command for LangSmith tracing management.

    Args:
        cmd_args: Command arguments (e.g., ["status"], ["enable"], ["projects"])

    Returns:
        True (always handled)
    """
    from novacode_cli.tracking.tracing import (
        apply_langsmith_env,
        get_traces,
        get_tracing_config,
        get_tracing_status,
        list_projects,
        persist_langsmith_settings,
    )

    console.print()

    # Parse subcommand
    subcmd = cmd_args[0].lower() if cmd_args else "status"

    if subcmd == "status":
        # Show current tracing status
        status = get_tracing_status()

        header = Text()
        header.append("📊 ", style="bold")
        header.append("LangSmith Tracing Status", style=f"bold {COLORS['primary']}")

        if status["available"]:
            if status["configured"]:
                config = get_tracing_config()
                console.print(
                    Panel(
                        f"✅ LangSmith tracing is [bold]ENABLED[/bold]\n\n"
                        f"Project: {config.project_name}\n"
                        f"Workspace: {config.workspace_id or '[dim]default[/dim]'}\n"
                        f"\n[dim]View traces at:[/dim]\n"
                        f"[link=https://smith.langchain.com]https://smith.langchain.com[/link]",
                        title=header,
                        border_style=COLORS["primary"],
                        padding=(1, 2),
                    )
                )
            else:
                console.print(
                    Panel(
                        "⚠️  LangSmith tracing is [bold]NOT CONFIGURED[/bold]\n\n"
                        "To enable tracing:\n"
                        "1. Set LANGSMITH_API_KEY environment variable\n"
                        "2. Set LANGSMITH_TRACING=true\n"
                        "3. Optionally set LANGSMITH_PROJECT for custom project name",
                        title=header,
                        border_style=COLORS["warning"],
                        padding=(1, 2),
                    )
                )
        else:
            console.print(
                Panel(
                    Text(
                        "❌ LangSmith library is not installed.\n\n"
                        "Install with: uv add langsmith",
                        style="dim",
                    ),
                    title=header,
                    border_style=COLORS["error"],
                    padding=(1, 2),
                )
            )

    elif subcmd in ("setup", "config"):
        _setup_tracing_interactively()

    elif subcmd in ("enable", "on"):
        # Enable tracing
        api_key = cmd_args[1] if len(cmd_args) > 1 else None
        project_name = None
        for i, arg in enumerate(cmd_args):
            if arg == "--project" and i + 1 < len(cmd_args):
                project_name = cmd_args[i + 1]
                break

        # Only the values that were given are changed: an existing API key or
        # project in the environment stays in use.
        values = {"LANGSMITH_TRACING": "true"}
        if api_key:
            values["LANGSMITH_API_KEY"] = api_key
        if project_name:
            values["LANGSMITH_PROJECT"] = project_name
        persist_langsmith_settings(values)
        config = apply_langsmith_env(values)

        if config.is_configured():
            console.print(
                "✅ [bold]LangSmith tracing enabled[/bold]",
                style=COLORS["success"],
            )
            console.print(f"   Project: {config.project_name}")
            console.print("   New prompts will be traced to this project.")
        else:
            console.print(
                "❌ [bold]Failed to enable tracing[/bold]",
                style=COLORS["error"],
            )
            console.print("   LANGSMITH_API_KEY is required to enable tracing.")

    elif subcmd in ("disable", "off"):
        # Disable tracing
        # The helper also clears LangSmith's cached environment, so the tracer
        # is really detached and not just flagged off.
        apply_langsmith_env({"LANGSMITH_TRACING": "false"})
        persist_langsmith_settings({"LANGSMITH_TRACING": "false"})
        console.print(
            "✅ [bold]LangSmith tracing disabled[/bold]", style=COLORS["success"]
        )
        console.print(
            "   [dim]This setting will stay off in future sessions.[/dim]"
        )

    elif subcmd == "projects":
        # List tracing projects
        projects = list_projects()

        header = Text()
        header.append("📁 ", style="bold")
        header.append("LangSmith Projects", style=f"bold {COLORS['primary']}")

        if projects:
            from rich.table import Table

            table = Table(show_header=True, header_style="bold")
            table.add_column("Project")
            table.add_column("URL")

            for p in projects[:20]:  # Limit to 20
                table.add_row(p["name"], p["url"])

            console.print(Panel(table, title=header, border_style=COLORS["primary"]))
        else:
            console.print(
                Panel(
                    Text("No projects found or tracing not configured.", style="dim"),
                    title=header,
                    border_style=COLORS["dim"],
                )
            )

    elif subcmd in ("traces", "recent"):
        # Show recent traces
        limit = 10
        for i, arg in enumerate(cmd_args):
            if arg in ("-n", "--limit") and i + 1 < len(cmd_args):
                try:
                    limit = int(cmd_args[i + 1])
                except ValueError:
                    pass

        traces = get_traces(limit=limit)

        header = Text()
        header.append("🧵 ", style="bold")
        header.append(
            f"Recent Traces (last {limit})", style=f"bold {COLORS['primary']}"
        )

        if traces:
            from rich.table import Table

            table = Table(show_header=True, header_style="bold")
            table.add_column("Name")
            table.add_column("Created")
            table.add_column("Inputs", width=40)

            for t in traces[:10]:
                created = t.get("created_at", "unknown")[:19] or "unknown"
                inputs = str(t.get("inputs", {}))[:40]
                table.add_row(t["name"], created, inputs)

            console.print(Panel(table, title=header, border_style=COLORS["primary"]))
        else:
            console.print(
                Panel(
                    Text(
                        "No traces found. Make a request with tracing enabled first.",
                        style="dim",
                    ),
                    title=header,
                    border_style=COLORS["dim"],
                )
            )

    elif subcmd in ("-h", "--help", "help"):
        # Show help for trace command
        header = Text()
        header.append("🔧 ", style="bold")
        header.append("/trace Command Help", style=f"bold {COLORS['primary']}")

        console.print(
            Panel(
                Text(
                    "/trace - Manage LangSmith tracing for debugging and observability\n\n"
                    "[bold]Subcommands:[/bold]\n"
                    "  setup       Enter the LangSmith environment and activate tracing\n"
                    "  status      Show current tracing configuration\n"
                    "  enable      Enable tracing (optionally with API key and project name)\n"
                    "              Usage: /trace enable [API_KEY] [--project PROJECT_NAME]\n"
                    "  disable     Disable tracing for current session\n"
                    "  projects    List all projects in LangSmith\n"
                    "  traces      Show recent traces\n"
                    "              Usage: /trace traces [--limit N]\n"
                    "  help        Show this help message\n\n"
                    "[bold]Environment Variables:[/bold]\n"
                    "  LANGSMITH_TRACING     Set to 'true' to enable tracing\n"
                    "  LANGSMITH_ENDPOINT    LangSmith API endpoint\n"
                    "  LANGSMITH_API_KEY     Your LangSmith API key\n"
                    "  LANGSMITH_PROJECT     Project name (default: 'Nova-Code')\n"
                    "  LANGSMITH_WORKSPACE_ID Workspace ID for multi-tenant setups\n\n"
                    "[bold]Links:[/bold]\n"
                    "  📊 LangSmith Dashboard: https://smith.langchain.com\n"
                    "  📚 Docs: https://docs.smith.langchain.com",
                    style="dim",
                ),
                title=header,
                border_style=COLORS["primary"],
                padding=(1, 2),
            )
        )

    else:
        console.print(f"[yellow]Unknown trace subcommand: {subcmd}[/yellow]")
        console.print("[dim]Use /trace help for available commands.[/dim]")

    console.print()
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _setup_tracing_interactively() -> None:
    """Ask for the LangSmith environment and activate tracing with it."""
    from novacode_cli.tracking.tracing import (
        LANGSMITH_ENV_VARS,
        apply_langsmith_env,
        persist_langsmith_settings,
    )

    current = {name: os.environ.get(name, "") for name in LANGSMITH_ENV_VARS}
    values: dict[str, str] = {}
    for name, prompt in (
        ("LANGSMITH_TRACING", "Enable tracing? (true/false)"),
        ("LANGSMITH_ENDPOINT", "LangSmith endpoint"),
        ("LANGSMITH_API_KEY", "LangSmith API key"),
        ("LANGSMITH_PROJECT", "Project name"),
        ("LANGSMITH_WORKSPACE_ID", "Workspace ID (optional; type 'clear' to remove)"),
        ("LANGSMITH_AGENT_ID", "Agent ID label (optional)"),
        ("LANGSMITH_AGENT_ENVIRONMENT", "Agent environment label (optional)"),
    ):
        default = current.get(name, "")
        try:
            if name == "LANGSMITH_API_KEY":
                marker = "set; Enter to keep" if default else "not set"
                answer = console.input(f"{prompt} [{marker}]: ", password=True).strip()
            else:
                answer = console.input(f"{prompt} [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("Setup cancelled.", style=COLORS["warning"])
            return
        # An empty answer keeps the current value. Optional fields accept an
        # explicit clear command to remove stale settings.
        if name in {
            "LANGSMITH_WORKSPACE_ID",
            "LANGSMITH_AGENT_ID",
            "LANGSMITH_AGENT_ENVIRONMENT",
        } and answer.lower() == "clear":
            values[name] = ""
        else:
            values[name] = answer or default

    warnings = persist_langsmith_settings(values)
    config = apply_langsmith_env(values)
    if config.is_configured():
        console.print(
            "✅ [bold]LangSmith tracing enabled[/bold]", style=COLORS["success"]
        )
        console.print(f"   Project: {config.project_name}")
        console.print("   New prompts will be traced to this project.")
        if config.endpoint:
            console.print(f"   Endpoint: {config.endpoint}")
        for warning in warnings:
            console.print(f"   ⚠ {warning}", style=COLORS["warning"])
    else:
        console.print(
            "⚠️  [bold]LangSmith tracing is not active[/bold]",
            style=COLORS["warning"],
        )
        console.print("   An API key is required to enable tracing.")


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        args_list = ctx.cmd_args.split() if ctx.cmd_args else []
        return await handle_trace_command(args_list)

    registry.register("trace", _handle)
