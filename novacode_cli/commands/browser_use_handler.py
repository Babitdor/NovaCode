"""Handler for /browser-use command - AI-powered browser automation.

This module provides browser automation using the browser_use library
with Ollama as the LLM provider and vision capabilities.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.text import Text

from novacode_cli.config.config import COLORS, console
from novacode_cli.ui.ui_elements import TokenTracker

if TYPE_CHECKING:
    from novacode_cli.session.session import SessionState

# Constants
ERROR_PREVIEW_LENGTH = 100
DEFAULT_MODEL = "qwen3.5:cloud"


class BrowserUseTask:
    """Represents a browser-use task for tracking."""

    def __init__(
        self,
        task_id: str,
        task_description: str,
        model: str = DEFAULT_MODEL,
        *,
        use_vision: bool = True,
    ) -> None:
        """Initialize a browser-use task."""
        self.task_id = task_id
        self.task_description = task_description
        self.model = model
        self.use_vision = use_vision
        self.status = "pending"
        self.result: str | None = None
        self.error_message: str | None = None
        self.created_at = datetime.now(UTC)
        self.completed_at: datetime | None = None


def _print_usage_help() -> None:
    """Print usage help for browser-use command."""
    console.print()
    console.print("[yellow]Usage: /browser-use <task> [--model M] [--no-vision][/yellow]")
    console.print("[yellow]       /browser-use --status[/yellow]")
    console.print()
    console.print("[dim]Example: /browser-use Go to github.com and find trending repos[/dim]")
    console.print("[dim]        /browser-use Search for Python tutorials --model llama3.2[/dim]")
    console.print("[dim]        /browser-use Fill out the contact form --no-vision[/dim]")
    console.print()
    console.print("[bold]Options:[/bold]")
    console.print("  --model M, -m M       Ollama model to use (default: qwen3.5:cloud)")
    console.print("  --no-vision           Disable vision capability")
    console.print("  --status              Show status of running browser tasks")
    console.print()
    console.print(
        "[dim]Browser-use mode runs AI-powered browser automation "
        "with vision capabilities.[/dim]"
    )
    console.print(
        "[dim]Requires Ollama to be running with the specified model "
        "installed.[/dim]"
    )
    console.print()


def _parse_browser_use_args(cmd_args: str | list[str] | None) -> tuple[str, str, bool] | None:
    """Parse browser-use command arguments.

    Args:
        cmd_args: Command arguments (task description and optional flags).

    Returns:
        Tuple of (task, model, use_vision) or None if parsing fails.
    """
    if not cmd_args:
        return None

    # Convert cmd_args from string to list if needed
    args_list = cmd_args.split() if isinstance(cmd_args, str) else list(cmd_args)

    if not args_list:
        return None

    # Check for --status flag
    if len(args_list) == 1 and args_list[0] == "--status":
        return ("--status", "", False)

    # Parse task, model, and vision flag
    model = DEFAULT_MODEL
    use_vision = True
    task_parts = []
    i = 0
    while i < len(args_list):
        arg = args_list[i]
        if arg in ("--model", "-m"):
            if i + 1 < len(args_list):
                model = args_list[i + 1]
                i += 2
                continue
            # Missing model name
            console.print()
            console.print("[red]Error: --model requires a model name[/red]")
            console.print("[dim]Example: /browser-use task --model llama3.2[/dim]")
            console.print()
            return None
        if arg == "--no-vision":
            use_vision = False
            i += 1
            continue
        task_parts.append(arg)
        i += 1

    task = " ".join(task_parts)
    if not task:
        console.print()
        console.print("[red]Error: No task specified[/red]")
        console.print("[dim]Usage: /browser-use <task> [--model M] [--no-vision][/dim]")
        console.print()
        return None

    return (task, model, use_vision)


def _display_task_header(task: str, model: str, *, use_vision: bool) -> None:
    """Display the browser-use task header panel."""
    console.print()
    header = Text()
    header.append("🌐 ", style="bold")
    header.append("Browser-Use Mode", style=f"bold {COLORS['primary']}")
    panel_content = (
        f"[bold]Task:[/bold] {task}\n"
        f"[bold]Model:[/bold] {model}\n"
        f"[bold]Vision:[/bold] {'Enabled' if use_vision else 'Disabled'}"
    )

    console.print(
        Panel(
            panel_content,
            title=header,
            border_style=COLORS["primary"],
            padding=(1, 2),
        )
    )
    console.print()
    console.print("[dim]Starting browser automation...[/dim]")
    console.print("[dim]Press Ctrl+C to stop at any time.[/dim]")
    console.print()


def _display_result(result: str, elapsed: float) -> None:
    """Display the browser automation result."""
    console.print()
    console.print(f"[green]✓[/green] Browser automation completed in {elapsed:.1f}s")
    console.print()
    if result:
        console.print(Panel(result, title="Result", border_style=COLORS["primary"]))
    console.print()


def _display_error(error: Exception, model: str) -> None:
    """Display an error message for browser automation failure."""
    console.print()
    console.print(f"[red]Error: {error}[/red]")
    console.print()
    console.print("[dim]Make sure Ollama is running and the model is installed:[/dim]")
    console.print(f"[dim]  ollama pull {model}[/dim]")
    console.print()


async def handle_browser_use_command(
    agent: object,
    session_state: "SessionState",
    assistant_id: str,
    token_tracker: TokenTracker,
    cmd_args: str | list[str] | None,
    execute_fn=None,
) -> bool:
    """Handle /browser-use command - AI-powered browser automation.

    Usage:
        /browser-use <task>              - Run browser automation task
        /browser-use <task> --model M    - Use specific Ollama model (default: qwen3.5:cloud)
        /browser-use <task> --no-vision  - Disable vision capability
        /browser-use --status            - Show status of running browser tasks

    Args:
        agent: The agent to send results to for processing.
        session_state: Current session state for tracking tasks.
        assistant_id: The assistant ID for agent execution.
        token_tracker: Token tracker for the session.
        cmd_args: Command arguments (task description and optional flags).

    Returns:
        True if handled, 'exit' if user requests exit.
    """
    if execute_fn is None:
        from novacode_cli.ui.execution import execute_task

        execute_fn = execute_task

    # Parse arguments
    parsed = _parse_browser_use_args(cmd_args)
    if parsed is None:
        _print_usage_help()
        return True

    task, model, use_vision = parsed

    # Handle --status flag
    if task == "--status":
        return await _handle_browser_use_status(session_state)

    # Display header
    _display_task_header(task, model, use_vision=use_vision)

    # Create task for tracking
    task_id = str(uuid.uuid4())[:8]
    browser_task = BrowserUseTask(
        task_id=task_id,
        task_description=task,
        model=model,
        use_vision=use_vision,
    )

    # Initialize browser tasks dict in session state if needed
    if not hasattr(session_state, "browser_use_tasks"):
        session_state.browser_use_tasks = {}

    session_state.browser_use_tasks[task_id] = browser_task

    # Execute the browser-use task
    try:
        result = await _execute_browser_use_task(
            task=task,
            model=model,
            use_vision=use_vision,
            browser_task=browser_task,
        )

        browser_task.status = "completed"
        browser_task.result = result
        browser_task.completed_at = datetime.now(UTC)

        # Display result
        elapsed = (browser_task.completed_at - browser_task.created_at).total_seconds()
        _display_result(result, elapsed)

        # Send result to agent for processing
        if result:
            console.print()
            console.print("[dim]Sending browser results to agent for analysis...[/dim]")
            console.print()
            
            # Format the result as a message for the agent
            agent_message = (
                f"I completed the browser automation task: '{task}'\n\n"
                f"Browser automation result:\n{result}\n\n"
                f"Please analyze these results and let me know if you need any follow-up actions."
            )
            
            # Execute the agent with the browser results
            await execute_fn(
                user_input=agent_message,
                agent=agent,
                assistant_id=assistant_id,
                session_state=session_state,
                token_tracker=token_tracker,
            )

    except KeyboardInterrupt:
        browser_task.status = "cancelled"
        browser_task.completed_at = datetime.now(UTC)
        console.print()
        console.print("[yellow]Browser automation cancelled by user[/yellow]")
        console.print()

    except (ImportError, RuntimeError, OSError) as e:
        browser_task.status = "failed"
        browser_task.error_message = str(e)
        browser_task.completed_at = datetime.now(UTC)
        _display_error(e, model)

    return True


async def _execute_browser_use_task(
    task: str,
    model: str,
    *,
    use_vision: bool,
    browser_task: BrowserUseTask,
) -> str:
    """Execute a browser-use task using the browser_use library.

    Args:
        task: The task description for the browser to perform.
        model: The Ollama model to use.
        use_vision: Whether to enable vision capabilities.
        browser_task: The task object for status tracking.

    Returns:
        The result of the browser automation task.
    """
    browser_task.status = "running"

    # Import browser_use components
    try:
        from browser_use import Agent, ChatOllama
    except ImportError as e:
        msg = (
            f"/browser-use needs the optional `browser` extra ({e}).\n"
            "From a checkout:   uv sync --extra browser\n"
            "Installed tool:    uv tool install --force "
            '"novacode-cli[browser] @ git+https://github.com/Babitdor/NovaCode.git"'
        )
        raise ImportError(msg) from e

    # Create the browser-use ChatOllama model
    llm = ChatOllama(model=model)

    # Create the browser-use agent
    agent = Agent(
        task=task,
        llm=llm,
        use_vision=use_vision,
    )

    # Run the agent
    result = await agent.run()

    # Extract result string
    # final_result() is a method on AgentHistoryList
    if hasattr(result, "final_result"):
        final = result.final_result()
        if final:
            return final
    if hasattr(result, "content"):
        return str(result.content)
    return str(result)


async def _handle_browser_use_status(session_state: "SessionState") -> bool:
    """Handle --status flag for browser-use command.

    Args:
        session_state: Current session state with browser tasks.

    Returns:
        True always (command handled).
    """
    console.print()
    console.print("[bold]Browser-Use Tasks Status[/bold]")
    console.print()

    if not hasattr(session_state, "browser_use_tasks") or not session_state.browser_use_tasks:
        console.print("[dim]No browser-use tasks have been run in this session.[/dim]")
        console.print()
        return True

    # Display all tasks
    for task_id, task in session_state.browser_use_tasks.items():
        status_icon = {
            "pending": "⏳",
            "running": "🔄",
            "completed": "✅",
            "failed": "❌",
            "cancelled": "🚫",
        }.get(task.status, "❓")

        status_color = {
            "pending": "yellow",
            "running": "blue",
            "completed": "green",
            "failed": "red",
            "cancelled": "yellow",
        }.get(task.status, "white")

        elapsed = ""
        if task.completed_at:
            elapsed = f" (elapsed: {(task.completed_at - task.created_at).total_seconds():.1f}s)"

        console.print(
            f"  {status_icon} [{status_color}]{task.status}[/{status_color}] - "
            f"{task.task_description[:50]}...{elapsed}"
        )
        console.print(f"    Task ID: {task_id}")
        console.print(f"    Model: {task.model}, Vision: {'Yes' if task.use_vision else 'No'}")

        if task.error_message:
            error_preview = task.error_message[:ERROR_PREVIEW_LENGTH]
            if len(task.error_message) > ERROR_PREVIEW_LENGTH:
                error_preview += "..."
            console.print(f"    [red]Error: {error_preview}[/red]")

        console.print()

    # Summary
    total = len(session_state.browser_use_tasks)
    completed = sum(1 for t in session_state.browser_use_tasks.values() if t.status == "completed")
    failed = sum(1 for t in session_state.browser_use_tasks.values() if t.status == "failed")

    console.print(f"[bold]Summary:[/bold] {total} tasks, {completed} completed, {failed} failed")
    console.print()

    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_browser_use_command(
            ctx.agent, ctx.session_state, ctx.assistant_id,
            ctx.token_tracker, ctx.cmd_args,
        )

    registry.register("browser-use", _handle)

