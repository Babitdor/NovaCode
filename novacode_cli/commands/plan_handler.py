"""Handler for the /plan command to invoke the plan-mode agent.

This module handles:
1. Invoking the plan agent when /plan is used
2. Managing plan mode state transitions
3. User approval workflow before execution
4. Handing off approved plans to the Nova Agent
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from rich import box
from rich.panel import Panel

from novacode_cli.config.config import console, settings

if TYPE_CHECKING:
    from novacode_cli.session import SessionState  # type: ignore


# Tools allowed for the plan agent (read-only for investigation)
PLAN_AGENT_TOOLS = [
    "read_file",
    "ls",
    "glob",
    "grep",
    "git_diff",
    "web_search",
    "fetch_url",
    "docs_search",
    "ask_question",
    "exit_plan_mode",
]

# Tools for Nova Agent after plan approval (execution phase)
EXECUTION_AGENT_TOOLS = [
    "git_diff",
    "web_search",
    "fetch_url",
    "docs_search",
]


async def handle_plan_command(
    agent,
    session_state: "SessionState",
    args: str | None = None,
) -> bool:
    """Handle the /plan command to invoke the plan-mode agent.

    Usage:
        /plan                    - Start plan mode with current context
        /plan <prompt>           - Enable plan mode and send prompt to agent
        /plan status             - Show current plan mode status
        /plan off                - Disable plan mode (with approval)
        /plan list               - List archived plans across all projects

    Args:
        agent: The current agent (Nova Agent for execution).
        session_state: Current session state.
        args: Optional arguments (status, off, or a prompt).

    Returns:
        True if handled (no prompt to pass), False if prompt should be passed to agent.
    """
    if args is None or args.strip() == "":
        # Start plan mode - invoke plan agent
        return await _start_plan_mode(agent, session_state, None)

    arg = args.strip().lower()

    # Check if it's a control argument
    if arg == "status":
        status = "enabled" if session_state.plan_mode_enabled else "disabled"
        color = "cyan" if session_state.plan_mode_enabled else "yellow"
        console.print(f"[{color}]Plan Mode: {status}[/{color}]")
        console.print()
        return True

    if arg == "off":
        return await _disable_plan_mode(agent, session_state)

    if arg in ("list", "ls"):
        _show_plan_archive()
        return True

    # Treat it as a prompt for the plan agent
    return await _start_plan_mode(agent, session_state, args)


async def _start_plan_mode(
    agent,
    session_state: "SessionState",
    prompt: str | None,
) -> bool:
    """Start plan mode by invoking the plan agent.

    This creates a plan agent with read-only tools, runs it to create a plan,
    and then requires user approval before handing off to the Nova Agent.

    Args:
        agent: The Nova Agent (for execution after approval).
        session_state: Current session state.
        prompt: Optional prompt to send to the plan agent.

    Returns:
        True if handled completely, False if prompt should be passed to agent.
    """
    from novacode_cli.agents.plan_agent import create_plan_agent_with_config

    # Set plan mode flag and clear any old plan content from previous sessions
    session_state.plan_mode_enabled = True
    session_state.plan_content = None  # Clear old plan content
    session_state.approved_plan_content = None  # Clear any previously approved plan
    if not getattr(session_state, "auto_approve", False):
        session_state.auto_approve = False  # Reset auto-approve only if it was False

    console.print()
    console.print(
        Panel(
            "[bold]Plan Mode Activated[/bold]\n\n"
            "• Agent will investigate the codebase\n"
            "• Ask clarifying questions if needed\n"
            "• Require your approval before execution\n\n"
            "[dim]Only read-only tools are available during planning[/dim]",
            title="[cyan]Plan Mode[/cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )
    console.print()

    # Create the plan agent with restricted tools
    # The plan agent uses the same model as the Nova Agent
    # but with read-only tools and plan-mode middleware
    try:
        # Get the model from the current agent configuration
        # This is passed from the session
        model = getattr(session_state, "_model", None)

        if model is None:
            console.print(
                "[yellow]Warning: No model configured, using default[/yellow]"
            )
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(model_name="claude-sonnet-4-20250514")  # type: ignore

        from novacode_cli.tools.plan_mode_tools import (
            ask_user_question,
            enter_plan_mode,
            exit_plan_mode,
            recent_plans,
        )
        from novacode_cli.tools.web_tools import (
            web_search,
            docs_search,
            duckduckgo_search,
        )

        # Create plan agent. Share the core agent's checkpointer + store so plan
        # mode continues from the existing conversation (same thread_id) and
        # persists across restarts, instead of starting from an empty context.
        plan_agent, plan_backend = create_plan_agent_with_config(
            model=model,
            assistant_id=session_state._assistant_id or "nova",
            tools=[
                ask_user_question,
                enter_plan_mode,
                exit_plan_mode,
                recent_plans,
                web_search,
                docs_search,
                duckduckgo_search,
            ],
            steering_instructions=session_state.steering_instructions,
            auto_approve=getattr(session_state, "auto_approve", False),
            checkpointer=getattr(session_state, "_checkpointer", None),
            store=getattr(session_state, "_store", None),
        )

        # Store the plan agent in session state for later use
        session_state.plan_agent = plan_agent
        session_state.plan_backend = plan_backend

        # If a prompt was provided, return the prompt string to process it through the active agent
        if prompt:
            return prompt

        return True

    except Exception as e:
        console.print(f"[red]Error starting plan mode: {e}[/red]")
        session_state.plan_mode_enabled = False
        return True


async def _disable_plan_mode(
    agent,
    session_state: "SessionState",
) -> bool:
    """Disable plan mode and require user approval.

    Shows the plan for approval and, if approved, hands off to Nova Agent
    with execution tools.

    Args:
        agent: The Nova Agent (for execution after approval).
        session_state: Current session state.

    Returns:
        True if handled completely.
    """
    from novacode_cli.ui.question_prompt import prompt_for_plan_approval
    from novacode_cli.ui.interrupt_handlers import find_latest_plan_file

    # Get backend from session state for virtual path access
    backend = getattr(session_state, "_backend", None)

    # Priority 1: Use plan content from session state (current session)
    plan_content = None
    if hasattr(session_state, "plan_content") and session_state.plan_content:
        plan_content = session_state.plan_content
    else:
        # Priority 2: Read from latest plan file as fallback (may be stale from previous session)
        # Same root exit_plan_mode writes to (see _persist_approved_plan).
        plans_dir = settings.get_workspace_root() / ".nova" / "plans"
        plan_path = find_latest_plan_file(plans_dir, backend=backend)
        if plan_path:
            # Try reading through backend first, fall back to filesystem.
            if backend is not None:
                try:
                    from novacode_cli.utils.backend_paths import (
                        read_via_backend,
                        real_to_virtual_path,
                    )

                    virtual = real_to_virtual_path(
                        plan_path,
                        workspace_root=settings.project_root,
                    )
                    if virtual:
                        plan_content = read_via_backend(virtual, backend)
                except Exception:
                    pass
            if plan_content is None:
                try:
                    plan_content = plan_path.read_text(encoding="utf-8")
                except Exception:
                    pass

    if getattr(session_state, "auto_approve", False):
        await _save_plan_approval(plan_content, auto_approved=True)
        session_state.plan_mode_enabled = False
        console.print("[green]Plan Approved - switching to execution mode (auto-approved)[/green]")
        console.print()
        if plan_content:
            session_state.set_approved_plan(plan_content)
        session_state.clear_plan_agent()
        console.print("[cyan]Nova Agent ready for execution[/cyan]")
        console.print()
        return True

    # Show approval dialog
    result = prompt_for_plan_approval(
        todos=session_state.todos,
        plan_summary=plan_content
        or "No plan found. Create a plan first or use /plan <prompt>.",
    )

    if result["approved"]:
        await _save_plan_approval(plan_content, auto_approved=False)
        session_state.plan_mode_enabled = False
        console.print("[green]Plan Approved - switching to execution mode[/green]")
        console.print()

        # Store approved plan for Nova agent execution
        if plan_content:
            session_state.set_approved_plan(plan_content)

        # Clear the plan agent from session state
        session_state.clear_plan_agent()

        # The Nova Agent will now execute with full tools
        console.print("[cyan]Nova Agent ready for execution[/cyan]")
        console.print(
            "[dim]Available tools: git_diff, web_search, fetch_url, docs_search[/dim]"
        )
        console.print()
    else:
        console.print("[cyan]Plan rejected - staying in Plan Mode[/cyan]")
        console.print(
            "[dim]Edit your plan and try again, or use /plan off to exit[/dim]"
        )
        console.print()

    return True


async def _save_plan_approval(plan_content: str | None, *, auto_approved: bool) -> None:
    """Save through the shared writer without blocking approval on disk errors."""
    from novacode_cli.tools.plan_mode_tools import persist_approved_plan

    try:
        await asyncio.to_thread(
            persist_approved_plan, plan_content or "", auto_approved=auto_approved
        )
    except OSError:
        console.print("[yellow]The approved plan could not be saved to disk.[/yellow]")


async def handle_plan_approval(
    agent,
    session_state: "SessionState",
    plan_content: str,
) -> bool:
    """Handle plan approval after exit_plan_mode is called.

    This is called when the plan agent calls exit_plan_mode tool.
    It shows the plan for user approval and handles the transition.

    Args:
        agent: The Nova Agent (for execution after approval).
        session_state: Current session state.
        plan_content: The content of the plan.

    Returns:
        True if approved, False if rejected.
    """
    if getattr(session_state, "auto_approve", False):
        await _save_plan_approval(plan_content, auto_approved=True)
        session_state.plan_mode_enabled = False
        session_state.clear_plan_agent()
        console.print("[green]Plan Auto-Approved![/green]")
        console.print("[cyan]Switching to Nova Agent for execution[/cyan]")
        return True

    from novacode_cli.ui.question_prompt import prompt_for_plan_approval

    console.print()
    console.print(
        Panel(
            f"[bold]Plan Ready for Review[/bold]\n\n"
            f"{plan_content[:500]}{'...' if len(plan_content) > 500 else ''}",
            title="[cyan]Plan Approval[/cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    result = prompt_for_plan_approval(
        todos=session_state.todos,
        plan_summary=plan_content,
    )

    if result["approved"]:
        await _save_plan_approval(plan_content, auto_approved=False)
        session_state.plan_mode_enabled = False
        session_state.clear_plan_agent()
        console.print("[green]Plan Approved![/green]")
        console.print("[cyan]Switching to Nova Agent for execution[/cyan]")
        return True
    else:
        console.print("[yellow]Plan Rejected - please revise[/yellow]")
        return False


__all__ = [
    "handle_plan_command",
    "handle_plan_approval",
    "PLAN_AGENT_TOOLS",
    "EXECUTION_AGENT_TOOLS",
]


def _show_plan_archive(limit: int = 30) -> None:
    """Print every archived plan, newest first, grouped by origin project.

    Plans live with their project; the archive under ``~/.nova/plans/`` mirrors
    them so they can be browsed across checkouts. Plans written before the
    archive existed are backfilled on first listing.
    """
    from rich.table import Table

    from novacode_cli.plan_archive import backfill_from_project, list_archived_plans

    # Plans saved before the archive existed are otherwise invisible to it.
    try:
        backfill_from_project(settings.get_workspace_root())
    except Exception:  # noqa: BLE001 - listing must work even if this fails
        pass

    plans = list_archived_plans(limit=limit)
    console.print()
    if not plans:
        console.print("[dim]No archived plans yet.[/dim]")
        console.print("[dim]Plans are archived when you approve them via plan mode.[/dim]")
        console.print()
        return

    from datetime import datetime

    current = str(settings.get_workspace_root())
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Plan")
    table.add_column("Project")
    table.add_column("Saved", style="dim")

    for p in plans:
        # Mark the project in play so the list is scannable at a glance.
        here = p.project == current
        project = Path(p.project).name + (" [cyan](here)[/cyan]" if here else "")
        table.add_row(
            p.title,
            project,
            datetime.fromtimestamp(p.modified).strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)
    console.print()
    console.print(f"[dim]{len(plans)} plan(s) — archive: {settings.nova_dir / 'plans'}[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_plan_command(ctx.agent, ctx.session_state, ctx.cmd_args)

    registry.register("plan", _handle)
