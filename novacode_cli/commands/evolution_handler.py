"""Handler for the /evolution command — view the self-evolution log.

Shows how the agent has evolved: skills unlocked (🧬) and levelled up (⬆️) at
the completion of complex tasks, plus the running totals. Data is read from the
durable store namespaces the ``EvolutionEngine`` writes:
``("nova","evolution_log")`` and ``("nova","meta")/"evolution"``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from novacode_cli.config.config import COLORS, console

# A UI sink: called with one Rich-markup string per line ("" = blank line).
# Mirrors dream_handler so the TUI can render natively instead of printing to
# the Rich console (which corrupts the Textual display).
EmitFn = Callable[[str], None]


def _console_emit(message: str = "") -> None:
    """Default emitter (CLI/REPL): render a Rich-markup string to the console."""
    console.print(message)


async def _load_evolution(*, strict: bool = False) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Return ``(counters, entries)`` from the durable store (newest first)."""
    from novacode_cli.memory.store import get_durable_store

    store = await asyncio.to_thread(get_durable_store)

    counters = {"unlocked": 0, "leveled": 0}
    try:
        entry = await store.aget(("nova", "meta"), "evolution")
        if entry and isinstance(entry.value, dict):
            counters = {
                "unlocked": int(entry.value.get("unlocked", 0)),
                "leveled": int(entry.value.get("leveled", 0)),
            }
    except Exception:  # noqa: BLE001
        if strict:
            raise
        pass

    entries: list[dict[str, Any]] = []
    try:
        results = await store.asearch(("nova", "evolution_log"))
        for item in results or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                entries.append(dict(value))
    except Exception:  # noqa: BLE001
        if strict:
            raise
        pass
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return counters, entries


async def handle_evolution_command(
    limit: int = 15,
    emit: EmitFn | None = None,
) -> bool:
    """Render the evolution summary + recent unlocks/level-ups.

    Writes through ``emit`` (one Rich-markup line at a time) so the TUI can
    render natively; defaults to the Rich console for the CLI/REPL.
    """
    if emit is None:
        emit = _console_emit

    counters, entries = await _load_evolution()

    emit("")
    emit(f"[bold {COLORS['primary']}]🧬 Self-Evolution[/bold {COLORS['primary']}]")
    emit(
        f"  [green]{counters['unlocked']} unlocked[/green]   "
        f"[cyan]{counters['leveled']} levelled up[/cyan]"
    )

    if not entries:
        emit(
            "  [dim]No evolutions yet — finish a complex task "
            "(edits + tests, todos, or subagents) to unlock a skill.[/dim]"
        )
        emit("")
        return True

    emit("")
    for e in entries[:limit]:
        icon = "🧬" if e.get("kind") == "unlock" else "⬆️"
        verb = "unlocked" if e.get("kind") == "unlock" else "levelled up"
        skill = e.get("skill", "?")
        when = ""
        ts = e.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            when = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")
        summary = (e.get("task_summary") or "").strip()
        line = f"  {icon} [bold]{skill}[/bold] [dim]{verb}"
        if when:
            line += f" · {when}"
        line += "[/dim]"
        emit(line)
        if summary:
            emit(f"      [dim]task: {summary}[/dim]")
    emit("")
    return True


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:  # noqa: ARG001
        return await handle_evolution_command()

    registry.register("evolution", _handle)
