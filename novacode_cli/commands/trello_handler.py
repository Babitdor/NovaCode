"""Handler for the /trello command — a kanban task board in the browser.

Usage:
    /trello              - Start the board server and open the browser
    /trello stop         - Stop the board server
    /trello status       - Show the current board state

The board is a self-contained HTML page served from a local HTTP server.
Add tasks in the browser, then drag a card to "Processing" (or flip on
Auto-advance) and the agent picks it up, runs it, and stores the result on
the card. The watch loop processes one "processing" task at a time.
"""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Callable
from typing import Any

from novacode_cli.config.config import console
from novacode_cli.ui.ui_elements import TokenTracker


def _content_text(content: Any) -> str:
    """Flatten a LangChain message ``content`` (str or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


async def _capture_task_result(
    agent: Any, assistant_id: str | None, session_state: Any
) -> str | None:
    """Read the final assistant message from the persisted graph state.

    Mirrors the token-usage fallback: the streamed events don't return the
    response text, but the aggregated AIMessage in the checkpoint does.
    Best-effort — any failure just yields no result.
    """
    try:
        from novacode_cli.core.input_preparation import build_agent_config

        config = build_agent_config(session_state.thread_id, assistant_id)
        state = await agent.aget_state(config)
        for msg in reversed(state.values.get("messages", [])):
            is_ai = getattr(msg, "type", "") == "ai" or msg.__class__.__name__ == "AIMessage"
            if is_ai:
                text = _content_text(getattr(msg, "content", "")).strip()
                if text:
                    return text
    except Exception:
        return None
    return None


async def trello_watch_loop(
    server: Any,
    agent: Any,
    assistant_id: str | None,
    session_state: Any,
    token_tracker: TokenTracker | None,
    execute_fn: Any,
    log: Callable[..., None],
) -> None:
    """Process board tasks until the server stops.

    Picks the oldest "processing" task (FIFO); if none and auto-advance is on,
    pulls the next "loaded" card. Runs one task at a time, captures the agent's
    output, and marks the card done with that result.
    """
    while server.is_running:
        task = server.next_processing_task()
        if not task and getattr(server, "auto_advance", False):
            task = server.pop_next_loaded_task()
        if not task:
            await asyncio.sleep(0.5)
            continue

        server.set_running(task["id"])
        log(f"📋 Processing task: {task['description']}", "bold")
        result: str | None
        try:
            await execute_fn(task["description"], agent, assistant_id, session_state, token_tracker)
            result = await _capture_task_result(agent, assistant_id, session_state)
        except Exception as exc:  # noqa: BLE001 — surface failure on the card, keep the loop alive
            result = f"Error: {exc}"
        finally:
            server.set_running(None)

        server.mark_done(task["id"], result)
        log(f"✓ Task completed: {task['description']}", "green")


async def handle_trello_command(
    agent: Any,
    session_state: Any,
    assistant_id: str | None,
    token_tracker: TokenTracker | None = None,
    cmd_args: str | None = None,
    execute_fn: Any = None,
) -> bool:
    """Handle the /trello command (REPL path)."""
    from novacode_cli.commands.trello_server import TrelloServer

    if execute_fn is None:
        from novacode_cli.ui.execution import execute_task

        execute_fn = execute_task

    if cmd_args and cmd_args.strip() == "stop":
        return _handle_stop(session_state)

    if cmd_args and cmd_args.strip() == "status":
        return _handle_status(session_state)

    existing_server: TrelloServer | None = getattr(session_state, "trello_server", None)
    if existing_server and existing_server.is_running:
        console.print()
        console.print(
            f"[yellow]Kanban board is already running at "
            f"[cyan]{existing_server.url}[/cyan][/yellow]"
        )
        console.print("[dim]Use /trello stop to stop it.[/dim]")
        console.print()
        return True

    server = TrelloServer()
    await server.start()
    from novacode_cli.commands.trello_remote import connect_telegram
    server.bind_remote(lambda: connect_telegram(session_state), str(session_state.session_id))
    session_state.trello_server = server

    try:
        webbrowser.open(f"{server.url}")
    except Exception:
        pass  # Browser opening is best-effort

    console.print()
    console.print(f"[green]✓[/green] Kanban board started at [cyan]{server.url}[/cyan]")
    console.print(
        "[dim]Add tasks, then drag a card to Processing (or toggle Auto-advance). "
        "The agent processes them one at a time.[/dim]"
    )
    console.print("[dim]Use /trello stop to shut down the server.[/dim]")
    console.print()

    def _log(message: str, style: str = "") -> None:
        console.print()
        console.print(f"[{style}]{message}[/{style}]" if style else message)

    await trello_watch_loop(
        server, agent, assistant_id, session_state, token_tracker, execute_fn, _log
    )
    return True


def _handle_stop(session_state: Any) -> bool:
    """Handle /trello stop - shut down the server."""
    server: Any = getattr(session_state, "trello_server", None)
    if server and server.is_running:
        server.stop()
        session_state.trello_server = None
        console.print()
        console.print("[green]✓[/green] Kanban board server stopped.")
        console.print()
    else:
        console.print()
        console.print("[yellow]No kanban board server is running.[/yellow]")
        console.print()
    return True


def _handle_status(session_state: Any) -> bool:
    """Handle /trello status - show current board state."""
    server: Any = getattr(session_state, "trello_server", None)
    if not server or not server.is_running:
        console.print()
        console.print("[yellow]No kanban board server is running.[/yellow]")
        console.print("[dim]Start one with /trello[/dim]")
        console.print()
        return True

    counts = server.get_task_counts()
    processing_task = server.get_processing_task()

    console.print()
    console.print("[bold]📋 Kanban Board Status[/bold]")
    console.print(f"  Server: [cyan]{server.url}[/cyan]")
    console.print(f"  Auto-advance: [bold]{'on' if server.auto_advance else 'off'}[/bold]")
    console.print(f"  📥 Loaded:    [bold]{counts['loaded']}[/bold]")
    console.print(f"  ⚙️ Processing: [bold]{counts['processing']}[/bold]")
    console.print(f"  ✅ Done:      [bold]{counts['done']}[/bold]")
    if processing_task:
        running = " [cyan](running)[/cyan]" if server.running_id == processing_task["id"] else ""
        console.print(f"  Currently: [dim]{processing_task['description']}[/dim]{running}")
    console.print()
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_trello_command(
            agent=ctx.agent,
            session_state=ctx.session_state,
            assistant_id=ctx.assistant_id,
            token_tracker=ctx.token_tracker,
            cmd_args=ctx.cmd_args,
        )

    registry.register("trello", _handle)
