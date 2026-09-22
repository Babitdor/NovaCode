"""Handlers for session-related commands: /sessions, /save, /compact."""

from pathlib import Path
from prompt_toolkit import PromptSession

from novacode_cli.commands import CommandContext
from novacode_cli.commands.menu_helper import MenuOption, run_interactive_menu
from novacode_cli.config.config import COLORS, console, settings
from novacode_cli.ui.ui_elements import TokenTracker
from novacode_cli.utils.model_info import get_current_provider


async def handle_sessions_command(ctx: CommandContext) -> bool:
    """Handle the /sessions command — delegates to interactive menu."""
    options = [
        MenuOption("List saved sessions", _action_list_sessions),
        MenuOption("Delete a session", _action_delete_session),
    ]
    return await run_interactive_menu("Session Management", options, ctx)


async def _action_list_sessions(
    ctx: CommandContext, session: PromptSession,
) -> bool:
    """Option 1: list saved sessions."""
    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.session.session_restore import format_session_age

    session_state = ctx.session_state
    session_manager = SessionManager()
    sessions = session_manager.list_sessions(limit=20)

    console.print()
    if sessions:
        console.print("[bold]Saved Sessions:[/bold]", style=COLORS["primary"])
        console.print()
        for meta in sessions:
            age = format_session_age(meta.last_active)
            project = (
                Path(meta.project_root).name if meta.project_root else "no project"
            )
            model = meta.model_name or "unknown model"
            is_current = session_state.session_id == meta.session_id
            marker = " ← current" if is_current else ""

            console.print(
                f"  • [bold]{meta.session_id[:8]}[/bold]{marker}",
                style=COLORS["primary"],
            )
            console.print(
                f"    {project} ({model}), {meta.message_count} messages",
                style=COLORS["dim"],
            )
            console.print(f"    {age}", style=COLORS["dim"])
            console.print()
    else:
        console.print("[yellow]No saved sessions found[/yellow]")
        console.print("[dim]Sessions are saved automatically on exit[/dim]")
    return True


async def _action_delete_session(
    ctx: CommandContext, session: PromptSession,
) -> bool:
    """Option 2: delete a session."""
    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.session.session_restore import format_session_age

    session_manager = SessionManager()
    sessions = session_manager.list_sessions(limit=20)

    if not sessions:
        console.print()
        console.print("[yellow]No sessions to delete[/yellow]")
        return True

    console.print()
    console.print("[bold]Select session to delete:[/bold]", style=COLORS["primary"])
    for i, meta in enumerate(sessions, 1):
        age = format_session_age(meta.last_active)
        project = (
            Path(meta.project_root).name if meta.project_root else "no project"
        )
        console.print(f"  {i}. {meta.session_id[:8]} - {project} ({age})")

    console.print()
    delete_choice = (
        await session.prompt_async("Choose session number (or 'cancel'): ")
    ).strip()

    if delete_choice.lower() != "cancel":
        try:
            delete_idx = int(delete_choice) - 1
            if 0 <= delete_idx < len(sessions):
                meta = sessions[delete_idx]
                confirm = (
                    (
                        await session.prompt_async(
                            f"Delete session {meta.session_id[:8]}? (y/N): ",
                            default="n",
                        )
                    )
                    .strip()
                    .lower()
                )

                if confirm == "y":
                    if session_manager.delete_session(meta.session_id):
                        console.print()
                        console.print(
                            f"✓ Session {meta.session_id[:8]} deleted",
                            style=COLORS["primary"],
                        )
                    else:
                        console.print()
                        console.print("[red]Failed to delete session[/red]")
                else:
                    console.print()
                    console.print("[yellow]Cancelled[/yellow]")
            else:
                console.print()
                console.print("[yellow]Invalid choice[/yellow]")
        except (ValueError, IndexError):
            console.print()
            console.print("[yellow]Invalid choice[/yellow]")
    return True


async def handle_save_command(
    agent,
    session_state,
    assistant_id: str,
    session_manager=None,
    model_name: str | None = None,
    sandbox_id: str | None = None,
    sandbox_type: str | None = None,
) -> bool:
    """Handle the /save command - manually save current session.

    Args:
        agent: The LangGraph agent
        session_state: Current session state
        assistant_id: Agent identifier
        session_manager: Session manager instance
        model_name: Name of the model being used
        sandbox_id: Sandbox/container ID for session reconnect
        sandbox_type: Sandbox provider ("docker", "modal", ...) or None

    Returns:
        True (command always handled)
    """
    if session_manager is None:
        from novacode_cli.session.session_persistence import SessionManager

        session_manager = SessionManager()

    console.print()

    try:
        # Get current messages from agent state
        config = {"configurable": {"thread_id": session_state.thread_id}}
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])

        if not messages:
            console.print("[yellow]No conversation to save yet[/yellow]")
            console.print()
            return True

        # The project root, so /save and --continue agree from any subfolder.
        project_root = settings.get_workspace_root()

        # Save the session (session_id is always set in SessionState.__init__)
        session_dir = session_manager.save_session(
            session_id=session_state.session_id,
            thread_id=session_state.thread_id,
            messages=messages,
            assistant_id=assistant_id,
            todos=session_state.todos,
            model_name=model_name,
            model_provider=get_current_provider(),
            project_root=project_root,
            sandbox_id=sandbox_id,
            sandbox_type=sandbox_type,
        )

        console.print(
            f"✓ Session saved: {session_state.session_id[:8]}...",
            style=COLORS["primary"],
        )
        console.print(f"  [dim]{len(messages)} messages saved to {session_dir}[/dim]")
        console.print("  [dim]Use 'nova --continue' to resume this session[/dim]")

    except Exception as e:
        console.print(f"[red]Failed to save session: {e}[/red]")

    console.print()
    return True


async def handle_compact_command(
    agent,
    session_state,
    token_tracker: TokenTracker,
    focus_instructions: str | None = None,
) -> bool:
    """Handle the /compact command to summarize conversation history.

    Args:
        agent: The LangGraph agent
        session_state: Current session state
        token_tracker: Token tracker instance
        focus_instructions: Optional user instructions (e.g., "Focus on X and Y")

    Returns:
        True (command always handled)
    """
    from novacode_cli.compaction import compact_conversation
    from novacode_cli.config.model_create import create_model

    console.print()
    console.print("[bold]Compacting Conversation[/bold]", style=COLORS["primary"])
    console.print()

    # Summarize with the session's live model when available (honors /model), else config.
    model = getattr(session_state, "_model", None) or create_model()

    with console.status("[bold]Summarizing conversation...[/bold]", spinner="dots"):
        result = await compact_conversation(
            agent=agent,
            model=model,
            thread_id=session_state.thread_id,
            focus_instructions=focus_instructions,
        )

    if result.success:
        console.print("[green]✓[/green] ", end="")
        console.print("[green]Conversation compacted successfully![/green]")
        console.print()

        # Show statistics
        console.print(
            f"  [dim]Messages: {result.messages_before} → {result.messages_after}[/dim]"
        )
        console.print(f"  [dim]Tokens saved: ~{result.tokens_saved:,}[/dim]")
        console.print()

        # Show summary preview (first 500 chars)
        console.print("[bold]Summary Preview:[/bold]", style=COLORS["primary"])
        preview = result.summary[:500]
        if len(result.summary) > 500:
            preview += "..."
        console.print(f"[dim]{preview}[/dim]")
        console.print()

        # Reset token tracker counters
        token_tracker.reset()

        # Fire compact hook
        try:
            from novacode_cli.hooks import dispatch_hook_fire_and_forget, HookEvent
            dispatch_hook_fire_and_forget(HookEvent.COMPACT, {
                "messages_before": result.messages_before,
                "messages_after": result.messages_after,
                "tokens_saved": result.tokens_saved,
                "session_id": session_state.session_id,
            })
        except Exception:
            pass

    else:
        console.print("[red]✗[/red] ", end="")
        console.print(f"[red]Compaction failed: {result.error}[/red]")
        console.print()

    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle_sessions(ctx: CommandContext) -> bool:
        return await handle_sessions_command(ctx)

    async def _handle_save(ctx: CommandContext) -> bool:
        return await handle_save_command(
            ctx.agent, ctx.session_state, ctx.assistant_id,
            ctx.session_manager, ctx.model_name,
            ctx.sandbox_id, ctx.sandbox_type,
        )

    async def _handle_compact(ctx: CommandContext) -> bool:
        return await handle_compact_command(
            ctx.agent, ctx.session_state, ctx.token_tracker,
            focus_instructions=ctx.cmd_args,
        )

    registry.register("sessions", _handle_sessions)
    registry.register("save", _handle_save)
    registry.register("compact", _handle_compact)
