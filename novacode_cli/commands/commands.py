from novacode_cli.remote.config import save_remote_config
"""Command handlers for slash commands and bash execution.

This module provides the main handle_command function that routes
slash commands via ``CommandRegistry`` to their respective handlers.
"""

import argparse
import asyncio
import io
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from novacode_cli.commands import (
    CommandContext,
    CommandRegistry,
    build_command_registry,
)
from novacode_cli.commands.ralph_handler import _stop_and_save_all_ralph_tasks
from novacode_cli.commands.skill_invoke import _try_skill_invocation
from novacode_cli.config.config import COLORS, NOVA_CODE_ASCII, console
from novacode_cli.states.Session import RalphTaskStatus
from novacode_cli.ui.ui_elements import TokenTracker, show_interactive_help

# Module-level registry, lazily populated on first use
_registry = None


def _get_registry() -> CommandRegistry:
    global _registry
    if _registry is None:
        _registry = build_command_registry()
    return _registry


@contextmanager
def silent_console_mode():
    """Context manager that suppresses all Rich console output.

    Works by redirecting the console's file handle to /dev/null.
    This suppresses all console output including print, status, tables, etc.
    """
    from novacode_cli.config.config import console

    # Save the original file handle
    original_file = console.file

    try:
        # Redirect console output to a black hole (StringIO that's never read)
        console.file = io.StringIO()
        yield
    finally:
        # Restore the original file handle
        console.file = original_file


async def handle_command(
    command: str,
    agent,
    token_tracker: TokenTracker,
    session_state,
    assistant_id: str,
    session_manager=None,
    model_name: str | None = None,
    image_tracker=None,
    sandbox_id: str | None = None,
    sandbox_type: str | None = None,
) -> str | bool:
    """Handle slash commands. Returns 'exit' to exit, True if handled, False to pass to agent."""
    # Parse command and optional arguments
    cmd_parts = command.strip().lstrip("/").split(maxsplit=1)
    cmd = cmd_parts[0].lower()
    cmd_args = cmd_parts[1] if len(cmd_parts) > 1 else None

    # /skill:<name> syntax — extract skill name and route to skill invocation
    if cmd.startswith("skill:"):
        _skill_name = cmd[len("skill:"):]
        _skill_args = cmd_args
        if not _skill_name:
            console.print()
            console.print("[yellow]Usage: /skill:<name> [args][/yellow]")
            console.print("[dim]Example: /skill:api-testing[/dim]")
            console.print()
            return True
        try:
            skill = await _try_skill_invocation(
                _skill_name, _skill_args, session_state, assistant_id
            )
            if skill is not None:
                # Render the invocation feedback (the resolver is presentation-free).
                console.print()
                console.print(
                    f"[bold {COLORS['primary']}]⚡ Invoking skill: {skill.name}"
                    f"[/bold {COLORS['primary']}]"
                )
                console.print(f"   [dim]{skill.description}[/dim]")
                console.print(f"   [dim]Source: {skill.source}[/dim]")
                if skill.args:
                    console.print(f"   [dim]Arguments: {skill.args}[/dim]")
                if skill.supporting_files:
                    console.print(
                        f"   [dim]Supporting files: "
                        f"{', '.join(skill.supporting_files)}[/dim]"
                    )
                console.print()
                return skill.prompt
        except Exception as e:
            console.print(f"[red]Error running /skill:{_skill_name}: {e}[/red]")
            return True
        console.print()
        console.print(f"[yellow]Unknown skill: {_skill_name}[/yellow]")
        console.print("[dim]Use /skills to list available skills.[/dim]")
        console.print()
        return True

    if cmd in ["quit", "exit", "q"]:
        # Check if Ralph is still running
        running_ralph_tasks = [
            task
            for task in session_state.background_ralph_tasks.values()
            if task.status == RalphTaskStatus.RUNNING
        ]

        if running_ralph_tasks:
            print()
            print("⚠️  Ralph is still running in the background")
            print()

            # Show which tasks are running
            for task in running_ralph_tasks:
                elapsed = (datetime.now(UTC) - task.created_at).total_seconds()
                print(
                    f"  ⏳ Iteration {task.iteration}/{task.max_iterations} "
                    f"(elapsed: {elapsed:.0f}s)"
                )
                task_desc = (
                    task.task_description[:50] + "..."
                    if len(task.task_description) > 50
                    else task.task_description
                )
                print(f"  📝 {task_desc}")

            print()
            print("What would you like to do?")
            print(
                "  1 - Stop Ralph and save checkpoint "
                "(resume later with /ralph --resume)"
            )
            print("  2 - Keep Ralph running and exit anyway")
            print("  3 - Cancel exit and continue")
            print()
            sys.stdout.flush()

            # Get user choice with validation using async-safe approach
            loop = asyncio.get_event_loop()
            choice = None
            while True:
                try:
                    choice = await loop.run_in_executor(
                        None, input, "Enter your choice (1-3): "
                    )
                    choice = choice.strip()
                    if choice in ["1", "2", "3"]:
                        break
                    print("Invalid choice. Please enter 1, 2, or 3.")
                    sys.stdout.flush()
                except EOFError:
                    return True

            print()
            sys.stdout.flush()

            if choice == "1":
                if _stop_and_save_all_ralph_tasks(session_state):
                    console.print()
                    console.print(
                        "[green]✓[/green] Ralph has been stopped and state saved"
                    )
                    console.print(
                        "[dim]You can resume your work anytime with: /ralph --resume[/dim]"
                    )
                    console.print()
                    return "exit"
            elif choice == "2":
                print("Ralph will continue running in the background.")
                print()
                sys.stdout.flush()
                return "exit"
            else:
                print("Exit cancelled. Continuing...")
                print()
                sys.stdout.flush()
                return True

        return "exit"

    # ── Local commands (handled here, not in registry) ─────────────────
    if cmd == "clear":
        # Mark the current session cleared so --continue won't auto-resume it
        # (it stays on disk / in the picker), then total-reset to a fresh one.
        if session_manager is not None:
            try:
                session_manager.mark_cleared(session_state.session_id)
            except Exception:  # noqa: BLE001
                pass
        # Total reset: new thread+session id (empty checkpointer context) plus
        # cleared todos / steering / plan mode. Long-term memory, the Nova
        # learning store, and the agent itself are preserved.
        session_state.reset_conversation()
        # Re-own the live sandbox to the new session (registry is the source of
        # truth for ownership; the Docker label is immutable) so resume reconnects
        # and the orphan sweep won't reclaim a container the new chat still uses.
        if sandbox_id:
            try:
                from novacode_cli.integrations import sandbox_registry

                sandbox_registry.retie(sandbox_id, session_state.session_id)
            except Exception:  # noqa: BLE001
                pass
        token_tracker.reset(reset_session=True)
        console.clear()
        from novacode_cli.brand import get_accent_hex

        console.print(NOVA_CODE_ASCII, style=f"bold {get_accent_hex()}")
        console.print()
        console.print(
            "... Fresh start! Conversation history cleared.",
            style=COLORS["agent"],
        )
        console.print()
        return True

    if cmd == "help":
        show_interactive_help()
        return True

    if cmd == "tokens":
        token_tracker.display_session()
        return True

    if cmd == "context":
        try:
            token_tracker.display_context()
        except Exception as e:
            console.print(f"[red]Error running /context command: {e}[/red]")
        return True

    if cmd == "verbose":
        new_state = session_state.toggle_verbose()
        if new_state:
            console.print()
            console.print(
                "  [bold green]Verbose mode enabled[/bold green] — "
                "internal agent context will be shown"
            )
            console.print()
        else:
            console.print()
            console.print(
                "  [bold yellow]Verbose mode disabled[/bold yellow] — "
                "internal agent context will be collapsed"
            )
            console.print()
        return True

    if cmd == "steer":
        try:
            return await _handle_steer_command(cmd_args, session_state, console)
        except Exception as e:
            console.print(f"[red]Error running /steer command: {e}[/red]")
        return True

    if cmd == "remote":
        try:
            return await _handle_remote_command(cmd_args, session_state, console)
        except Exception as e:
            import traceback
            console.print(f"[red]Error running /remote command: {e}[/red]")
            traceback.print_exc()
        return True

    if cmd == "cron":
        try:
            from novacode_cli.commands.cron_handler import handle_cron_command

            return await handle_cron_command(cmd_args, session_state, console)
        except Exception as e:
            console.print(f"[red]Error running /cron command: {e}[/red]")
        return True

    if cmd == "webhook":
        try:
            from novacode_cli.commands.webhook_handler import handle_webhook_command

            return await handle_webhook_command(cmd_args, session_state, console)
        except Exception as e:
            console.print(f"[red]Error running /webhook command: {e}[/red]")
        return True

    if cmd == "prompt":
        try:
            from novacode_cli.commands.prompt_handler import handle_prompt_command

            return await handle_prompt_command(cmd_args, session_state, console)
        except Exception as e:
            console.print(f"[red]Error running /prompt command: {e}[/red]")
        return True

    if cmd == "refine":
        try:
            from novacode_cli.commands.refine_handler import handle_refine_command

            return await handle_refine_command(cmd_args, session_state, console)
        except Exception as e:
            console.print(f"[red]Error running /refine command: {e}[/red]")
        return True

    if cmd == "voice":
        try:
            from novacode_cli.commands.voice_handler import handle_voice_command

            return await handle_voice_command(cmd_args, session_state, console)
        except Exception as e:
            console.print(f"[red]Error running /voice command: {e}[/red]")
        return True

    if cmd == "reindex":
        try:
            from novacode_cli.config.config import settings as _settings
            from novacode_cli.tools.code_search_tools import (
                _get_index,
                _is_semble_available,
                _reset_index,
            )
            if not _is_semble_available():
                console.print()
                console.print("[yellow]Code search is not available.[/yellow]")
                console.print(
                    "[dim]Install the 'semble' package to enable semantic "
                    "code search:[/dim]"
                )
                console.print("[dim]  pip install semble[/dim]")
                console.print()
            else:
                _reset_index()
                workspace = _settings.get_workspace_root()
                with console.status(
                    f"[bold {COLORS['primary']}]Re-indexing codebase...[/]",
                    spinner="dots",
                ):
                    idx = _get_index(workspace)
                if idx is not None:
                    console.print(
                        "[green]\u2713[/green] Code search index rebuilt for "
                        f"[cyan]{workspace}[/cyan]"
                    )
                else:
                    console.print(
                        "[red]Failed to build code search index.[/red]"
                    )
                console.print()
        except Exception as e:
            console.print(f"[red]Error running /reindex command: {e}[/red]")
        return True

    # ── Dispatch through registry ──────────────────────────────────────
    # Build a CommandContext once and route to the registered handler.
    # Special-cased commands (exit, skill) are handled above; everything
    # else goes through the registry with a single try/except block.
    ctx = CommandContext(
        cmd=cmd,
        cmd_args=cmd_args,
        agent=agent,
        token_tracker=token_tracker,
        session_state=session_state,
        assistant_id=assistant_id,
        session_manager=session_manager,
        model_name=model_name,
        image_tracker=image_tracker,
        sandbox_id=sandbox_id,
        sandbox_type=sandbox_type,
    )

    handler = _get_registry().get(cmd)
    if handler is not None:
        try:
            return await handler(ctx)
        except Exception as e:
            console.print(f"[red]Error running /{cmd} command: {e}[/red]")
            return True

    # Unknown command
    console.print()
    console.print(f"[yellow]Unknown command: /{cmd}[/yellow]")
    console.print("[dim]Type /help for available commands.[/dim]")
    console.print()
    return True


def execute_bash_command(command: str) -> bool:
    """Execute a bash command and display output. Returns True if handled."""
    cmd = command.strip().lstrip("!")

    if not cmd:
        return True

    try:
        console.print()
        console.print(f"[dim]$ {cmd}[/dim]")

        from novacode_cli.config.config import settings

        result = subprocess.run(
            cmd,
            check=False,
            shell=True,
            capture_output=True,
            timeout=30,
            cwd=settings.get_workspace_root(),
        )

        stdout = (result.stdout or b"").decode("utf-8", errors="replace")
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")

        if stdout.strip():
            console.print(stdout, style=COLORS["dim"], markup=False)
        if stderr.strip():
            console.print(stderr, style="red", markup=False)

        if result.returncode != 0:
            console.print(f"[dim]Exit code: {result.returncode}[/dim]")

        console.print()
        return True

    except subprocess.TimeoutExpired:
        console.print("[red]Command timed out after 30 seconds[/red]")
        console.print()
        return True
    except Exception as e:
        console.print(f"[red]Error executing command: {e}[/red]")
        console.print()
        return True


def execute_skills_command(args: argparse.Namespace) -> None:
    """Execute skills subcommands based on parsed arguments.

    Args:
        args: Parsed command line arguments with skills_command attribute
    """
    from novacode_cli.skills.skill_creation import (
        _add,
        _create,
        _find,
        _info,
        _list,
        _remove,
        _update,
        _validate_name,
    )

    if args.agent:
        is_valid, error_msg = _validate_name(args.agent)
        if not is_valid:
            console.print(
                f"[bold red]Error:[/bold red] Invalid agent name: {error_msg}"
            )
            console.print(
                "[dim]Agent names must only contain letters, numbers, "
                "hyphens, and underscores.[/dim]",
                style=COLORS["dim"],
            )
            return

    if args.skills_command == "list":
        _list(
            agent=args.agent,
            project=args.project,
            global_scope=getattr(args, "global_scope", False),
        )
    elif args.skills_command == "create":
        _create(
            args.name,
            agent=args.agent,
            project=args.project,
            global_scope=getattr(args, "global_scope", False),
        )
    elif args.skills_command == "info":
        _info(
            args.name,
            agent=args.agent,
            project=args.project,
            global_scope=getattr(args, "global_scope", False),
        )
    elif args.skills_command == "add":
        _add(
            args.url,
            agent=args.agent,
            project=args.project,
            global_scope=getattr(args, "global_scope", False),
            force=getattr(args, "force", False),
            skill_name=getattr(args, "skill_name", None),
        )
    elif args.skills_command == "remove":
        _remove(
            args.name,
            agent=args.agent,
            project=args.project,
            global_scope=getattr(args, "global_scope", False),
            yes=getattr(args, "yes", False),
        )
    elif args.skills_command == "update":
        _update(
            skill_name=getattr(args, "name", None),
            agent=args.agent,
            project=args.project,
            global_scope=getattr(args, "global_scope", False),
            all_skills=getattr(args, "all_skills", False),
        )
    elif args.skills_command in ("find", "search"):
        _find(args.query)
    else:
        console.print("[yellow]Please specify a skills subcommand.[/yellow]")
        console.print("\n[bold]Usage:[/bold]", style=COLORS["primary"])
        console.print("  Nova skills <command> [options]\n")
        console.print("[bold]Available commands:[/bold]", style=COLORS["primary"])
        console.print("  list                    List all available skills")
        console.print("  create <name>           Create a new skill")
        console.print(
            "  info <name>             Show detailed information about a skill"
        )
        console.print("  add <url>               Install a skill from GitHub URL")
        console.print("  remove <name>           Remove an installed skill")
        console.print(
            "  update [name]           Update skill(s) from their original source"
        )
        console.print("  find <query>            Search GitHub for skills")
        console.print("  search <query>          Alias for find")
        console.print("\n[bold]Examples:[/bold]", style=COLORS["primary"])
        console.print(
            "  Nova skills add https://github.com/owner/repo --skill my-skill"
        )
        console.print("  Nova skills remove my-skill -y")
        console.print("  Nova skills update my-skill")
        console.print("  Nova skills update --all --global")
        console.print("  Nova skills find kubernetes")
        console.print(
            "\n[dim]For more help on a specific command:[/dim]", style=COLORS["dim"]
        )
        console.print("  Nova skills <command> --help", style=COLORS["dim"])


async def _handle_steer_command(cmd_args: str | None, session_state, console) -> bool:
    """Handle the /steer command for managing persistent steering instructions.

    Subcommands:
        /steer <instruction>     Add a new steering instruction
        /steer list              Show all active steering instructions
        /steer clear             Remove all steering instructions
        /steer remove <N>        Remove instruction number N

    If no subcommand is given, defaults to 'list'.

    Args:
        cmd_args: Arguments after /steer.
        session_state: Current session state.
        console: Rich console for output.

    Returns:
        True (command was handled).
    """
    from novacode_cli.bootstrap.steering import (
        SteeringInstruction,
        classify_instruction,
        format_steering_status,
    )

    args = (cmd_args or "").strip()

    # Initialize the list if it doesn't exist yet
    if not hasattr(session_state, "steering_instructions"):
        session_state.steering_instructions = []

    instructions = session_state.steering_instructions

    # No args or "list" → show current instructions
    if not args or args.lower() in ("list", "ls", "show"):
        console.print()
        console.print(format_steering_status(instructions))
        console.print()
        console.print("[dim]Usage: /steer <instruction>  |  /steer remove <N>  |  /steer clear[/dim]")
        console.print()
        return True

    # "clear" → remove all
    if args.lower() in ("clear", "reset", "clear all"):
        if instructions:
            count = len(instructions)
            instructions.clear()
            console.print()
            console.print(f"  [green]\u2713[/green] Cleared {count} steering instruction(s)")
            console.print()
        else:
            console.print()
            console.print("[dim]No steering instructions to clear.[/dim]")
            console.print()
        return True

    # "remove <N>" → remove by index
    remove_match = re.match(r"(?:remove|rm|delete|del)\s+(\d+)", args, re.IGNORECASE)
    if remove_match:
        idx = int(remove_match.group(1)) - 1  # 1-based to 0-based
        if 0 <= idx < len(instructions):
            removed = instructions.pop(idx)
            console.print()
            console.print(
                f"  [green]\u2713[/green] Removed: [bold]{removed.label}[/bold]: {removed.instruction}"
            )
            console.print()
        else:
            console.print()
            console.print(f"  [yellow]Invalid index: {idx + 1}. Use /steer list to see numbered items.[/yellow]")
            console.print()
        return True

    # Otherwise, treat the entire args as a new instruction
    label = classify_instruction(args)
    instructions.append(SteeringInstruction(label=label, instruction=args))

    console.print()
    console.print(
        f"  [green]\u2713[/green] Added steering instruction: [bold]{label}[/bold]"
    )
    console.print(f"  {args}")
    console.print()
    console.print(
        f"  [dim]{len(instructions)} instruction(s) active. "
        "These will be injected into every model call this session.[/dim]"
    )
    console.print()
    return True


async def _handle_remote_command(cmd_args: str | None, session_state, console) -> bool:
    """Handle the /remote command for managing remote bridges.

    Tokens and IDs are saved to ~/.nova/remote.json so they can be reused
    without re-typing.  Use /remote forget to delete them.
    """
    from novacode_cli.remote.bridge import RemoteBridgeManager
    from novacode_cli.remote.config import (
        async_load_remote_config,
        async_save_discord_config,
        async_save_telegram_config,
    )

    # Get or create the bridge manager on the session state
    if session_state._remote_bridge_manager is None:
        queue = getattr(session_state, "_remote_message_queue", None)
        if queue is None:
            queue = asyncio.Queue()
            session_state._remote_message_queue = queue
        session_state._remote_bridge_manager = RemoteBridgeManager(queue)

    manager: RemoteBridgeManager = session_state._remote_bridge_manager
    args = (cmd_args or "").strip()

    # ── /remote (no args) or /remote status ────────────────────────
    if not args or args.lower() in ("status", "list", "ls"):
        bridges = manager.active_bridges
        saved = await async_load_remote_config()
        console.print()
        if not bridges:
            console.print("[dim]No remote bridges active.[/dim]")
        else:
            console.print(f"[cyan]{len(bridges)} bridge(s) active:[/cyan]")
            for b in bridges:
                status = b["status"]
                if status == "running":
                    icon = "\U0001f7e2"
                elif "error" in status.lower():
                    icon = "\U0001f534"
                else:
                    icon = "\U0001f7e1"
                bot_info = ""
                if b.get("bot_user"):
                    bot_info = f" (@{b['bot_user']})"
                console.print(
                    f"  {icon} [bold]{b['platform']}[/bold]{bot_info} "
                    f"\u2014 chat: {b['chat_id']} \u2014 {status}"
                )

        # Show saved config
        if saved:
            console.print()
            console.print("[dim]Saved configuration:[/dim]")
            if "discord" in saved:
                d = saved["discord"]
                has_token = "\u2713" if d.get("token") else "\u2717"
                has_channel = "\u2713" if d.get("channel_id") else "\u2717"
                console.print(f"  [dim]Discord:  token {has_token}  channel {has_channel} {d.get('channel_id', '')}[/dim]")
            if "telegram" in saved:
                t = saved["telegram"]
                has_token = "\u2713" if t.get("token") else "\u2717"
                has_chat = "\u2713" if t.get("chat_id") else "\u2717"
                console.print(f"  [dim]Telegram: token {has_token}  chat_id {has_chat} {t.get('chat_id', '')}[/dim]")

        console.print()
        console.print("[dim]Quick start:[/dim]")
        console.print("  [dim]/remote start discord    (uses saved token & channel)[/dim]")
        console.print("  [dim]/remote start discord --token TOKEN --channel ID[/dim]")
        console.print("  [dim]/remote start telegram --token TOKEN --chat ID[/dim]")
        console.print("  [dim]/remote test  |  /remote stop  |  /remote forget[/dim]")

        if not ("discord" in saved and saved.get("discord", {}).get("token")):
            console.print()
            console.print("[dim]Discord setup:[/dim]")
            console.print("  [dim]1. Create app at https://discord.com/developers/applications[/dim]")
            console.print("  [dim]2. Add Bot, enable [bold]Message Content Intent[/bold][/dim]")
            console.print("  [dim]3. Generate token, invite bot to server[/dim]")
        console.print()
        return True

    # ── /remote stop ──────────────────────────────────────────────
    if args.lower() in ("stop", "stop all"):
        await manager.stop_all()
        # Restore auto-approve to its pre-remote state
        if session_state._pre_remote_auto_approve is not None:
            session_state.auto_approve = session_state._pre_remote_auto_approve
            session_state._pre_remote_auto_approve = None
            if not session_state.auto_approve:
                console.print("  [dim]Auto-approve restored to off.[/dim]")
        console.print()
        console.print("  [green]\u2713[/green] All remote bridges stopped")
        console.print()
        return True

    # ── /remote stop BRIDGE_ID ─────────────────────────────────────
    stop_match = re.match(r"stop\s+(\S+)", args, re.IGNORECASE)
    if stop_match:
        bridge_id = stop_match.group(1)
        await manager.stop_bridge(bridge_id)
        # If no more active bridges, restore auto-approve
        if not manager.active_bridges and session_state._pre_remote_auto_approve is not None:
            session_state.auto_approve = session_state._pre_remote_auto_approve
            session_state._pre_remote_auto_approve = None
            if not session_state.auto_approve:
                console.print("  [dim]Auto-approve restored to off.[/dim]")
        console.print()
        console.print(f"  [green]\u2713[/green] Bridge {bridge_id} stopped")
        console.print()
        return True

    # ── /remote test ──────────────────────────────────────────────
    if args.lower().startswith("test"):
        bridges = manager.active_bridges
        if not bridges:
            console.print()
            console.print("[yellow]No active bridges to test.[/yellow]")
            console.print("  [dim]Start a bridge first: /remote start discord[/dim]")
            console.print()
            return True

        test_msg = "\u2709 [Remote Bridge Test] If you can see this, the bridge is working!"
        sent = False
        for b in bridges:
            bridge_id = b["id"]
            entry_dict = manager._bridges.get(bridge_id, {})
            bridge_obj = entry_dict.get("bridge")
            if bridge_obj is None:
                continue

            if b["platform"] == "discord" and hasattr(bridge_obj, "_client") and bridge_obj._client:
                client = bridge_obj._client
                channel_id = int(b["chat_id"])
                channel = client.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await client.fetch_channel(channel_id)
                    except Exception as e:
                        console.print(f"  [red]Channel {channel_id} not found: {e}[/red]")
                        continue
                if channel:
                    try:
                        await channel.send(test_msg)
                        console.print(f"  [green]\u2713[/green] Test message sent to Discord channel {channel_id}")
                        sent = True
                    except Exception as e:
                        console.print(f"  [red]Failed to send: {e}[/red]")

            elif b["platform"] == "telegram" and hasattr(bridge_obj, "_session"):
                config = entry_dict.get("config")
                if config:
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            url = f"https://api.telegram.org/bot{config.token}/sendMessage"
                            payload = {"chat_id": config.chat_id, "text": test_msg}
                            async with session.post(url, json=payload) as resp:
                                data = await resp.json()
                                if data.get("ok"):
                                    console.print(f"  [green]\u2713[/green] Test sent to Telegram chat {config.chat_id}")
                                    sent = True
                                else:
                                    desc = data.get("description", "Unknown error")
                                    console.print(f"  [red]Telegram API error: {desc}[/red]")
                    except Exception as e:
                        console.print(f"  [red]Failed to send: {e}[/red]")

        if not sent and bridges:
            console.print("  [yellow]Could not send through any bridge. Try /remote status.[/yellow]")
        console.print()
        return True

    # ── /remote forget [discord|telegram] ──────────────────────────
    forget_match = re.match(r"forget(?:\s+(discord|telegram))?$", args, re.IGNORECASE)
    if forget_match:
        platform = (forget_match.group(1) or "").lower()
        saved = await async_load_remote_config()
        if platform and platform in saved:
            del saved[platform]
            save_remote_config(saved)
            console.print()
            console.print(f"  [green]\u2713[/green] Removed saved {platform} configuration")
            console.print()
        elif not platform:
            save_remote_config({})
            console.print()
            console.print("  [green]\u2713[/green] Removed all saved remote configurations")
            console.print()
        else:
            console.print()
            console.print(f"  [yellow]No saved {platform} configuration to remove[/yellow]")
            console.print()
        return True

    # ── /remote start discord|telegram [--token T] [--channel ID|--chat ID] ──
    start_match = re.match(r"start\s+(discord|telegram)\s*(.*)", args, re.IGNORECASE)
    if start_match:
        platform_str = start_match.group(1).lower()
        rest = start_match.group(2).strip()

        # Parse --token and --channel/--chat from args
        token = None
        chat_id = None
        ping = None

        token_match = re.search(r"--token\s+(\S+)", rest)
        if token_match:
            token = token_match.group(1)

        if "--no-ping" in rest:
            ping = False
        elif "--ping" in rest:
            ping = True

        if platform_str == "discord":
            channel_match = re.search(r"--channel\s+(\d+)", rest)
            if channel_match:
                chat_id = channel_match.group(1)
        else:
            chat_match = re.search(r"(?:--chat|--chat-id)\s+(-?\d+)", rest)
            if chat_match:
                chat_id = chat_match.group(1)

        # ── Load saved config as fallback ──
        saved = await async_load_remote_config()

        if platform_str == "discord":
            saved_discord = saved.get("discord", {})
            if token is None:
                token = saved_discord.get("token")
            if chat_id is None:
                chat_id = saved_discord.get("channel_id")
            if ping is None:
                ping = saved_discord.get("ping", True)

            if not token:
                console.print()
                console.print("  [red]No Discord token provided or saved.[/red]")
                console.print("  [dim]Usage: /remote start discord --token <TOKEN> [--channel <ID>] [--ping|--no-ping][/dim]")
                console.print("  [dim]The token will be saved for future use.[/dim]")
                console.print()
                return True

            # Auto-create a per-session channel if not specified
            if not chat_id:
                from novacode_cli.config.config import settings

                project_slug = re.sub(
                    r"[^a-z0-9-]", "-", settings.get_workspace_root().name.lower().replace(" ", "-")
                ).strip("-") or "nova"
                # Short session suffix so each Nova session gets its own channel.
                session_suffix = (getattr(session_state, "session_id", "") or "")[:8]
                if not session_suffix:
                    session_suffix = uuid.uuid4().hex[:8]
                channel_name = f"{project_slug}-{session_suffix}"

                console.print()
                console.print(f"  No --channel specified. Creating #{channel_name} for this session...")

                success, error_msg = await manager.start_discord_auto_channel(
                    token=token, channel_name=channel_name, ping=ping
                )
                if not success:
                    console.print(f"  [red]✗[/red] Could not create channel: {error_msg}")
                    console.print("  [dim]Specify a channel ID with --channel <ID> instead.[/dim]")
                    console.print("  [dim]Tip: Right-click a Discord channel → Copy Channel ID[/dim]")
                    console.print()
                    return True

                # Get the channel ID from the bridge config
                for bid, entry in manager._bridges.items():
                    if bid.startswith("discord:"):
                        chat_id = entry["config"].chat_id
                        break

                if not chat_id:
                    console.print("  [red]✗[/red] Failed to get auto-created channel ID")
                    console.print()
                    return True

                console.print(f"  [green]✓[/green] Created session channel #{channel_name} (ID: {chat_id})")
                # Save only the token, not the channel ID — each session creates its own channel.
                await async_save_discord_config(token, "", ping=ping)

        elif platform_str == "telegram":
            saved_telegram = saved.get("telegram", {})
            if token is None:
                token = saved_telegram.get("token")
            if chat_id is None:
                chat_id = saved_telegram.get("chat_id")

            if not token:
                console.print()
                console.print("  [red]No Telegram token provided or saved.[/red]")
                console.print("  [dim]Usage: /remote start telegram --token <TOKEN> [--chat <ID>][/dim]")
                console.print("  [dim]The token will be saved for future use.[/dim]")
                console.print()
                return True

            if not chat_id:
                console.print()
                console.print("  [yellow]No --chat specified. Send any message to your Telegram bot,[/yellow]")
                console.print("  [yellow]then use /remote start telegram --chat <CHAT_ID>[/yellow]")
                console.print()
                return True

        # ── Save the config ──
        if platform_str == "discord":
            # channel_id may be "" when a per-session channel was auto-created above
            # (we don't persist the session channel, only the token).
            _channel_to_save = str(chat_id) if chat_id else ""
            await async_save_discord_config(token, _channel_to_save, ping=ping)
        else:
            await async_save_telegram_config(token, str(chat_id))

        # ── Start the bridge ──
        console.print()
        if platform_str == "discord":
            ping_status = "ping: enabled" if ping else "ping: disabled"
            console.print(f"  Starting Discord bridge (channel: {chat_id}, {ping_status})...")
        else:
            console.print(f"  Starting Telegram bridge (chat: {chat_id})...")

        try:
            if platform_str == "discord":
                success, error_msg = await manager.start_discord(token=token, channel_id=chat_id, ping=ping)
            else:
                success, error_msg = await manager.start_telegram(token=token, chat_id=int(chat_id))
        except Exception as e:
            import traceback
            console.print(f"  [red]\u2717[/red] Exception starting {platform_str.title()} bridge: {e}")
            traceback.print_exc()
            console.print()
            return True

        if success:
            bridge_id = f"{platform_str}:{chat_id}"
            bridge_info = manager.get_bridge(bridge_id)
            bot_user = ""
            if bridge_info and bridge_info.get("bot_user"):
                bot_user = f" (@{bridge_info['bot_user']})"
            console.print(f"  [green]\u2713[/green] {platform_str.title()} bridge started!{bot_user}")
            console.print("  [dim]Credentials saved to ~/.nova/remote.json[/dim]")
            if platform_str == "discord":
                console.print("  [dim]Make sure [bold]Message Content Intent[/bold] is enabled in your bot settings.[/dim]")

            # Telegram: try to create a per-session forum topic so each session
            # gets its own thread inside a forum supergroup.
            if platform_str == "telegram":
                from novacode_cli.config.config import settings

                _entry = manager._bridges.get(bridge_id, {})
                _tg_bridge = _entry.get("bridge")
                if _tg_bridge is not None:
                    _project_slug = re.sub(
                        r"[^a-z0-9-]", "-", settings.get_workspace_root().name.lower().replace(" ", "-")
                    ).strip("-") or "nova"
                    _session_suffix = (getattr(session_state, "session_id", "") or "")[:8]
                    _topic_name = f"Nova: {_project_slug} ({_session_suffix})"
                    try:
                        _thread_id = await _tg_bridge.create_forum_topic(int(chat_id), _topic_name)
                        if _thread_id is not None:
                            _tg_bridge._forum_thread_id = _thread_id
                            console.print(
                                f"  [dim]Forum topic created: {_topic_name} "
                                f"(thread {_thread_id})[/dim]"
                            )
                    except Exception:  # noqa: BLE001
                        pass  # non-forum groups just use the main chat

            # Auto-approve tool actions so remote messages don't block waiting
            # for local CLI input.  The processor also sets/restores this per-message,
            # but having it on persistently prevents any approval prompt from hanging.
            if not session_state.auto_approve:
                session_state._pre_remote_auto_approve = False  # was off before remote
                session_state.auto_approve = True
                console.print("  [dim]Auto-approve enabled for remote bridge.[/dim]")
        else:
            console.print(f"  [red]\u2717[/red] Failed to start {platform_str.title()} bridge: {error_msg}")
        console.print()
        return True

    # ── Unknown subcommand ───────────────────────────────────────
    console.print()
    console.print(f"  [yellow]Unknown /remote subcommand: {args[:40]}[/yellow]")
    console.print()
    console.print("[dim]Usage:[/dim]")
    console.print("  [dim]/remote start discord [--token TOKEN] [--channel ID][/dim]")
    console.print("  [dim]/remote start telegram [--token TOKEN] [--chat ID][/dim]")
    console.print("  [dim]/remote status  |  /remote test  |  /remote stop[/dim]")
    console.print("  [dim]/remote forget [discord|telegram][/dim]")
    console.print()
    return True
