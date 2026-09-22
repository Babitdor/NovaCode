"""Handlers for /agents command - custom agent management."""

import shutil
from pathlib import Path
from prompt_toolkit import PromptSession

from novacode_cli.commands import CommandContext
from novacode_cli.commands.menu_helper import MenuOption, run_interactive_menu
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
    ps = PromptSession()
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
        ctx = CommandContext(cmd="agents", cmd_args=cmd_args,
                             agent=None, token_tracker=None,
                             session_state=None, assistant_id=assistant_id)
        options = [
            MenuOption("View agents", _menu_view_agents),
            MenuOption("Create a new agent", _menu_create_agent),
            MenuOption("Delete an agent", _menu_delete_agent),
        ]
        return await run_interactive_menu("Agents Manager", options, ctx)

    if action == "view":
        return await _agents_list(settings)
    if action == "create":
        return await _agents_create_interactive(ps, settings)
    if action == "delete":
        return await _agents_delete_interactive(ps, settings)

    return True


async def _menu_view_agents(ctx: CommandContext, session: PromptSession) -> bool:
    return await _agents_list(Settings.from_environment())

async def _menu_create_agent(ctx: CommandContext, session: PromptSession) -> bool:
    return await _agents_create_interactive(session, Settings.from_environment())

async def _menu_delete_agent(ctx: CommandContext, session: PromptSession) -> bool:
    return await _agents_delete_interactive(session, Settings.from_environment())


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


async def _agents_create_interactive(ps: PromptSession, settings: Settings) -> bool:
    """Create a new custom agent interactively.

    Args:
        ps: PromptSession instance
        settings: Settings instance

    Returns:
        True (always handled)
    """
    console.print()
    console.print("[bold]Create New Agent[/bold]", style=COLORS["primary"])
    console.print()

    console.print(
        "[dim]Agents are specialized AI assistants with custom system prompts.[/dim]"
    )
    console.print(
        "[dim]They have full access to: file operations, shell commands, web search,[/dim]"
    )
    console.print("[dim]dev servers, test runners, and shared memory.[/dim]")
    console.print()
    console.print("[bold]Example agent types:[/bold]")
    console.print(
        "  • [cyan]code-reviewer[/cyan] - Reviews code for quality, security, best practices"
    )
    console.print("  • [cyan]debugger[/cyan] - Diagnoses and fixes bugs systematically")
    console.print(
        "  • [cyan]architect[/cyan] - Designs system architecture and patterns"
    )
    console.print("  • [cyan]test-writer[/cyan] - Creates comprehensive test suites")
    console.print(
        "  • [cyan]refactor-assistant[/cyan] - Improves code structure and readability"
    )
    console.print("  • [cyan]api-designer[/cyan] - Designs RESTful/GraphQL APIs")
    console.print(
        "  • [cyan]security-auditor[/cyan] - Identifies security vulnerabilities"
    )
    console.print("  • [cyan]performance-optimizer[/cyan] - Optimizes code performance")
    console.print()

    # Get agent name
    agent_name = (await ps.prompt_async("Agent name: ")).strip()

    if not agent_name:
        console.print("[yellow]Cancelled - no agent name provided[/yellow]")
        console.print()
        return True

    # Validate agent name
    if not settings._is_valid_agent_name(agent_name):
        console.print("[red]Invalid agent name.[/red]")
        console.print(
            "[dim]Use only letters, numbers, hyphens, underscores, and spaces.[/dim]"
        )
        console.print()
        return True

    # Ask for scope (global vs project)
    in_project = settings.project_root is not None
    use_project = False

    if in_project:
        console.print()
        console.print("[bold]Where should this agent be stored?[/bold]")
        console.print("  1. Global (available in all projects)")
        console.print("  2. Project (only available in this project)")
        console.print()
        scope_choice = (
            await ps.prompt_async("Scope (1-2, default=1): ")
        ).strip() or "1"
        use_project = scope_choice == "2"

    # Determine target directory based on scope
    if use_project:
        agents_dir = settings.ensure_project_agents_dir()
        if not agents_dir:
            console.print("[red]Error: Not in a project directory.[/red]")
            console.print()
            return True
        agent_dir = agents_dir / agent_name
        scope_label = "project"
    else:
        agent_dir = settings.get_agents_root_dir() / agent_name
        scope_label = "global"

    # Check if agent already exists in the chosen scope
    if agent_dir.exists():
        console.print(
            f"[yellow]Agent '{agent_name}' already exists at {agent_dir}[/yellow]"
        )
        console.print()
        return True

    # Also warn if agent exists in the other scope
    if use_project:
        global_agent_dir = settings.get_agents_root_dir() / agent_name
        if global_agent_dir.exists():
            console.print(
                f"[dim]Note: A global agent with the same name exists at {global_agent_dir}[/dim]"
            )
            console.print(
                "[dim]The project agent will take precedence when invoked from this project.[/dim]"
            )
            console.print()
    else:
        project_agents_dir = settings.get_project_agents_dir()
        if project_agents_dir:
            project_agent_dir = project_agents_dir / agent_name
            if project_agent_dir.exists():
                console.print(
                    f"[dim]Note: A project agent with the same name exists at {project_agent_dir}[/dim]"
                )
                console.print(
                    "[dim]The project agent will take precedence when invoked from this project.[/dim]"
                )
                console.print()

    # Get description
    console.print()
    console.print("[bold]Describe what this agent specializes in:[/bold]")
    console.print(
        "[dim]Be specific about the agent's focus area, expertise, and typical tasks.[/dim]"
    )
    console.print("[dim]Good examples:[/dim]")
    console.print(
        "[dim]  • 'Reviews Python code for security vulnerabilities, OWASP top 10, and secure coding practices'[/dim]"
    )
    console.print(
        "[dim]  • 'Creates and maintains React component tests using Jest and React Testing Library'[/dim]"
    )
    console.print(
        "[dim]  • 'Optimizes SQL queries and database schemas for PostgreSQL performance'[/dim]"
    )
    console.print()
    description = (await ps.prompt_async("Description: ")).strip()

    if not description:
        console.print("[yellow]Cancelled - no description provided[/yellow]")
        console.print()
        return True

    # Color selection
    console.print()
    console.print("[bold]Choose a color for this agent:[/bold]")
    color_options = [
        ("#ef4444", "Red"),
        ("#f97316", "Orange"),
        ("#f59e0b", "Amber"),
        ("#fbbf24", "Yellow"),
        ("#22c55e", "Green"),
        ("#14b8a6", "Teal"),
        ("#0ea5e9", "Sky Blue"),
        ("#3b82f6", "Blue"),
        ("#8b5cf6", "Violet"),
        ("#a855f7", "Purple"),
        ("#ec4899", "Pink"),
        ("#6b7280", "Gray"),
    ]
    for i, (hex_code, name) in enumerate(color_options, 1):
        console.print(f"  [{hex_code}]■[/{hex_code}] {i:2d}. {name} ({hex_code})")
    console.print()
    color_choice = (
        await ps.prompt_async("Color (1-12, or hex code, default=7): ")
    ).strip() or "7"

    # Parse color choice
    if color_choice.startswith("#"):
        agent_color = color_choice
    else:
        try:
            choice_idx = int(color_choice) - 1
            if 0 <= choice_idx < len(color_options):
                agent_color = color_options[choice_idx][0]
            else:
                agent_color = color_options[6][0]  # Default to Sky Blue
        except ValueError:
            agent_color = color_options[6][0]  # Default to Sky Blue

    # Generate system prompt using LLM
    console.print()
    console.print(
        "[dim]Generating comprehensive system prompt with tool guidelines...[/dim]"
    )

    system_prompt = await _generate_agent_system_prompt(agent_name, description)

    if not system_prompt:
        console.print("[red]Failed to generate system prompt.[/red]")
        console.print()
        return True

    # Show preview of generated prompt
    console.print()
    console.print("[bold]Generated System Prompt Preview:[/bold]")
    console.print("─" * 60)
    # Show first ~500 chars as preview
    preview = system_prompt[:500] + "..." if len(system_prompt) > 500 else system_prompt
    console.print(f"[dim]{preview}[/dim]")
    console.print("─" * 60)
    console.print()

    # Ask for confirmation
    confirm = (
        await ps.prompt_async("Create this agent? (y/n, default=y): ")
    ).strip().lower() or "y"
    if confirm not in ("y", "yes"):
        console.print("[yellow]Cancelled - agent not created[/yellow]")
        console.print()
        return True

    # Add YAML frontmatter with color
    final_content = f"""---
color: {agent_color}
description: {description}
---

{system_prompt}"""

    # Create agent directory and file
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_md = agent_dir / "agent.md"
    agent_md.write_text(final_content, encoding="utf-8")

    console.print()
    console.print(
        f"[green]✓ Agent '{agent_name}' created successfully! ({scope_label})[/green]"
    )
    console.print(f"[dim]Location: {agent_dir}[/dim]")
    console.print()
    console.print("[bold]How to use:[/bold]")
    console.print(
        f"  • Type [cyan]@{agent_name} <your query>[/cyan] to invoke this agent"
    )
    console.print(
        f"  • Run [cyan]Nova --agent {agent_name}[/cyan] to start with this agent"
    )
    console.print(f"  • Edit [cyan]{agent_md}[/cyan] to customize the prompt")
    console.print()
    return True


async def _agents_delete_interactive(ps: PromptSession, settings: Settings) -> bool:
    """Delete an existing agent from either global or project scope.

    Args:
        ps: PromptSession instance
        settings: Settings instance

    Returns:
        True (always handled)
    """
    console.print()
    console.print("[bold]Delete Agent[/bold]", style=COLORS["primary"])
    console.print()

    # Get all agents from both scopes
    all_agents = settings.get_all_agents()

    if not all_agents:
        console.print("[yellow]No agents found.[/yellow]")
        console.print()
        return True

    # Separate by scope for display
    project_agents = [
        (name, path) for name, path, scope in all_agents if scope == "project"
    ]
    global_agents = [
        (name, path) for name, path, scope in all_agents if scope == "global"
    ]

    # Build a combined list with scope labels
    agents_list: list[tuple[str, Path, str]] = []

    console.print("[bold]Available agents:[/bold]", style=COLORS["primary"])
    console.print()

    idx = 1
    if project_agents:
        console.print("[green]Project agents:[/green]")
        for name, path in sorted(project_agents):
            console.print(f"  {idx}. {name}")
            agents_list.append((name, path, "project"))
            idx += 1
        console.print()

    if global_agents:
        console.print("[cyan]Global agents:[/cyan]")
        for name, path in sorted(global_agents):
            console.print(f"  {idx}. {name}")
            agents_list.append((name, path, "global"))
            idx += 1
        console.print()

    choice = (
        await ps.prompt_async("Choose agent number to delete (or 'cancel'): ")
    ).strip()

    if choice.lower() == "cancel":
        console.print("[dim]Cancelled[/dim]")
        console.print()
        return True

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(agents_list):
            agent_name, agent_dir, scope = agents_list[idx]
            scope_label = "project" if scope == "project" else "global"

            # Confirm deletion
            confirm = (
                (
                    await ps.prompt_async(
                        f"Delete {scope_label} agent '{agent_name}'? This cannot be undone. (y/N): ",
                        default="n",
                    )
                )
                .strip()
                .lower()
            )

            if confirm == "y":
                shutil.rmtree(agent_dir)
                console.print()
                console.print(
                    f"[green]{scope_label.capitalize()} agent '{agent_name}' deleted.[/green]"
                )
            else:
                console.print()
                console.print("[dim]Cancelled[/dim]")
        else:
            console.print()
            console.print("[yellow]Invalid choice[/yellow]")
    except (ValueError, IndexError):
        console.print()
        console.print("[yellow]Invalid choice[/yellow]")

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
