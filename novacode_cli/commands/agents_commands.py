"""Handlers for /agents command - custom agent management."""

from pathlib import Path

from novacode_cli.config.config import COLORS, Settings, console
from novacode_cli.prompts import render_template


def extract_agent_description(agent_md: Path) -> str:
    """Extract description from agent.md file."""
    try:
        from novacode_cli.agents.agent_file import _split

        content = agent_md.read_text(encoding="utf-8")
        description = _split(content)[0].get("description")
        if description:
            return str(description).strip()[:80]

        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return (line[:80] + "...") if len(line) > 80 else line

    except Exception:
        pass

    return "[unable to read]"


async def handle_agents_command(cmd_args: str | None, assistant_id: str) -> bool:
    """Handle the /agents command."""
    settings = Settings.from_environment()

    console.print()
    console.print("[bold]Agents Manager[/bold]", style=COLORS["primary"])
    console.print()

    action = None

    if cmd_args:
        first_arg = cmd_args.strip().lower()
        if first_arg in ("view", "list", "ls", "show"):
            action = "view"
        elif first_arg in ("create", "new", "add"):
            action = "create"
        elif first_arg in ("delete", "remove", "rm"):
            action = "delete"

    if not action:
        # Interactive menu removed with the console REPL — point at the TUI.
        console.print("[yellow]This command is interactive in the TUI.[/yellow]")
        console.print(
            "[dim]Use /agents (the AgentsScreen) instead — "
            "the console REPL was removed.[/dim]"
        )
        console.print()
        return True

    if action == "view":
        return await _agents_list(settings)
    if action in ("create", "delete"):
        # Interactive create/delete removed with the console REPL — point at the TUI.
        console.print("[yellow]This command is interactive in the TUI.[/yellow]")
        console.print(
            "[dim]Use /agents (the AgentsScreen) instead — "
            "the console REPL was removed.[/dim]"
        )
        console.print()
        return True

    return True


async def _agents_list(settings: Settings) -> bool:
    """List all available custom agents from both global and project scopes.

    Args:
        settings: Settings instance

    Returns:
        True (always handled)
    """
    console.print()
    console.print("[bold]Available Agents:[/bold]", style=COLORS["primary"])
    console.print()

    # Get all agents from both scopes using the new Settings method
    all_agents = settings.get_all_agents()

    if not all_agents:
        console.print("[yellow]No agents found.[/yellow]")
        console.print("[dim]Use '/agents' to create a new agent.[/dim]")
        console.print()
        return True

    # Separate by scope
    global_agents = []
    project_agents = []

    for agent_name, agent_dir, scope in all_agents:
        agent_md = agent_dir / "agent.md"
        # Read first non-empty line for description
        description = extract_agent_description(agent_md)

        if scope == "project":
            project_agents.append((agent_name, description, agent_dir))
        else:
            global_agents.append((agent_name, description, agent_dir))

    # Display project agents first (they take precedence)
    if project_agents:
        console.print("[bold green]Project Agents:[/bold green]")
        console.print("[dim](Only available in this project)[/dim]")
        console.print()
        for name, description, _agent_dir in sorted(project_agents):
            console.print(f"  @[bold]{name}[/bold]", style=COLORS["primary"])
            if description:
                console.print(f"    [dim]{description}[/dim]")
        console.print()

    # Display global agents
    if global_agents:
        console.print("[bold cyan]Global Agents:[/bold cyan]")
        console.print("[dim](Available in all projects)[/dim]")
        console.print()
        for name, description, _agent_dir in sorted(global_agents):
            # Check if this global agent is shadowed by a project agent
            is_shadowed = any(pa[0] == name for pa in project_agents)
            if is_shadowed:
                console.print(
                    f"  @[bold]{name}[/bold] [dim](shadowed by project agent)[/dim]",
                    style=COLORS["primary"],
                )
            else:
                console.print(f"  @[bold]{name}[/bold]", style=COLORS["primary"])
            if description:
                console.print(f"    [dim]{description}[/dim]")
        console.print()

    total = len(global_agents) + len(project_agents)
    console.print(f"[dim]Total: {total} agent(s)[/dim]")
    console.print("[dim]Use @<agent_name> <query> to invoke an agent.[/dim]")
    console.print()
    return True


async def _generate_agent_system_prompt(
    agent_name: str, description: str
) -> str | None:
    """Generate a full system prompt for a custom agent using the configured LLM.

    Args:
        agent_name: Name of the agent
        description: Description of what the agent specializes in

    Returns:
        Generated system prompt, or None if generation failed
    """
    from novacode_cli.config.model_create import create_model

    try:
        model = create_model()

        # Generate agent system prompt using Jinja template
        generation_prompt = render_template(
            "agent_generation.jinja",
            agent_name=agent_name,
            description=description,
        )

        response = await model.ainvoke(generation_prompt)

        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Handle list of content blocks
                return "".join(str(c) for c in content)
        return str(response)

    except Exception as e:
        console.print(f"[red]Error generating prompt: {e}[/red]")
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_agents_command(cmd_args=ctx.cmd_args, assistant_id=ctx.assistant_id)

    registry.register("agents", _handle)
