"""Handler for the /dream command - memory consolidation.

This module provides the handle_dream_command function that runs
a memory consolidation pass to organize and clean up memory files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.config.config import COLORS, console
from novacode_cli.prompts import render_template

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry

_CONTEXT_TOPIC_PREVIEW = 8
_STATUS_TOPIC_PREVIEW = 6

# A UI sink: called with one Rich-markup string per line ("" = blank line).
EmitFn = Callable[[str], None]


def _console_emit(message: str = "") -> None:
    """Default emitter (CLI): render a Rich-markup string to the console."""
    console.print(message)


async def handle_dream_command(
    session_state: object,
    assistant_id: str | None = None,
    agent_dir: Path | None = None,
    emit: EmitFn | None = None,
) -> bool | str:
    """Handle the /dream command - run memory consolidation.

    Performs a multi-phase memory consolidation pass over the agent's *real*
    memory directory (``~/.nova/agents/{assistant_id}/``): agent.md (user model +
    preferences) and the topic files under ``memories/`` (indexed by INDEX.md).

    Returns the consolidation prompt (a ``str``) for the caller to stream to the
    agent when memory exists, or ``True`` (handled, nothing to do) when it does
    not. The agent acts on the **virtual** ``/memories/`` route — which maps to
    this directory — so its read/edit tools work regardless of OS path form.

    Args:
        session_state: Current session state.
        assistant_id: The active agent id (defaults to the main agent).
        agent_dir: Optional explicit agent directory (overrides the resolved one).
        emit: Optional renderer-specific status sink.

    Returns:
        The consolidation prompt string, or True if there is nothing to consolidate.
    """
    from novacode_cli.config.config import MAIN_AGENT_ID, settings

    if emit is None:
        emit = _console_emit
    if getattr(session_state, "plan_mode_enabled", False):
        emit("[yellow]Exit plan mode before running memory wiki maintenance.[/yellow]")
        return True

    # Resolve the REAL memory directory (the one Nova actually writes to).
    if agent_dir is None:
        try:
            agent_dir = settings.get_agent_dir(assistant_id or MAIN_AGENT_ID)
        except Exception:  # noqa: BLE001 - bad agent id, fall back to main
            agent_dir = settings.get_agent_dir(MAIN_AGENT_ID)

    # Nova's semantic surface: agent.md (user model + prefs) + topic files
    # under memories/ (indexed by INDEX.md).
    tier_files = {
        "agent.md": "user model, preferences & general notes",
        "HABITS.md": "stable operating checklist; incident lessons belong in topic memory",
    }
    memories_dir = agent_dir / "memories"

    present = [(name, role) for name, role in tier_files.items() if (agent_dir / name).exists()]
    topic_files = sorted(
        [
            *memories_dir.glob("*.md"),
            *(memories_dir / "projects").glob("*/*.md"),
        ]
    )

    if not present and not topic_files:
        emit("")
        emit("[yellow]No memory files found yet.[/yellow]")
        emit(f"[dim]Looked in: {agent_dir}[/dim]")
        emit(
            "[dim]Ask me to 'remember this' during a session and the memory tiers"
            " fill in; then /dream consolidates them.[/dim]"
        )
        emit("")
        return True

    transcripts_dir = agent_dir / "transcripts"

    # Describe Nova's concrete memory layout so the agent consolidates the right
    # files. Use the VIRTUAL /memories/ route — the agent's file tools resolve it
    # to the real agent dir; passing the raw OS path would be rejected by the
    # virtual-mode filesystem backend.
    lines = ["These memory files exist (operate on them under `/memories/`):"]
    for name, role in present:
        lines.append(f"- `/memories/{name}` — {role}")
    if topic_files:
        lines.append(
            f"- `/memories/memories/` — {len(topic_files)} topic file(s): "
            + ", ".join(f.name for f in topic_files[:_CONTEXT_TOPIC_PREVIEW])
            + ("…" if len(topic_files) > _CONTEXT_TOPIC_PREVIEW else "")
        )
    memory_dir_context = "\n".join(lines)

    # The wiki has one canonical home, including on its first maintenance pass.
    index_file = "/memories/memories/INDEX.md"

    from novacode_cli.memory.wiki import maintain_wiki, snapshot_wiki

    try:
        wiki = await asyncio.to_thread(maintain_wiki, agent_dir)
        backup = wiki["backup"] or str(await asyncio.to_thread(snapshot_wiki, agent_dir))
    except (OSError, ValueError, RuntimeError) as exc:
        emit(f"[yellow]Memory maintenance stopped: {exc}[/yellow]")
        return True

    emit("")
    emit(f"[dim]Wiki: {wiki['topics']} topics; {len(wiki['broken_links'])} unresolved links.[/dim]")
    emit(f"[bold {COLORS['primary']}]💭 Memory Consolidation (Dream)[/bold {COLORS['primary']}]")
    for name, role in present:
        emit(f"  [cyan]{name}[/cyan] [dim]— {role}[/dim]")
    if topic_files:
        names = ", ".join(f.name for f in topic_files[:_STATUS_TOPIC_PREVIEW]) + (
            "…" if len(topic_files) > _STATUS_TOPIC_PREVIEW else ""
        )
        emit(f"  [cyan]memories/[/cyan] [dim]— {len(topic_files)} topic file(s): {names}[/dim]")
    emit("")

    return render_template(
        "memory_consolidation.jinja",
        memory_dir="/memories/",
        memory_dir_context=memory_dir_context,
        index_file=index_file,
        index_max_lines=100,
        transcripts_dir="/memories/transcripts/" if transcripts_dir.exists() else None,
        backup_dir="/memories/" + Path(backup).relative_to(agent_dir).as_posix(),
    )


def get_dream_prompt(
    memory_dir: Path,
    transcripts_dir: Path | None = None,
    index_file: str = "/memories/memories/INDEX.md",
    index_max_lines: int = 100,
) -> str:
    """Generate the memory consolidation prompt.

    Args:
        memory_dir: Path to the memory directory
        transcripts_dir: Optional path to transcripts directory
        index_file: Virtual wiki home path.
        index_max_lines: Maximum lines for the index (default: 100)

    Returns:
        The rendered prompt string
    """
    return render_template(
        "memory_consolidation.jinja",
        memory_dir=memory_dir.as_posix().rstrip("/") + "/",
        memory_dir_context=f"Memory files are stored in {memory_dir}",
        index_file=index_file,
        index_max_lines=index_max_lines,
        transcripts_dir=str(transcripts_dir) if transcripts_dir else None,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_commands(registry: CommandRegistry) -> None:
    """Register the shared command handler for every renderer."""

    async def _handle(ctx: CommandContext) -> bool:
        result = await handle_dream_command(ctx.session_state, ctx.assistant_id)
        # When memory exists, the handler returns a consolidation prompt to run.
        if isinstance(result, str) and result.strip() and ctx.agent is not None:
            from novacode_cli.ui.execution import execute_task

            console.print("[dim]💭 Dreaming over memories…[/dim]")
            await execute_task(
                result,
                ctx.agent,
                ctx.assistant_id,
                ctx.session_state,
                ctx.token_tracker,
            )
        return True

    registry.register("dream", _handle)
