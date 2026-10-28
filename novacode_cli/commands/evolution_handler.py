"""Read-only learning activity for /evolution, including labelled legacy history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.markup import escape

from novacode_cli.config.config import COLORS, MAIN_AGENT_ID, console

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry

# A UI sink: called with one Rich-markup string per line ("" = blank line).
# Mirrors dream_handler so the TUI can render natively instead of printing to
# the Rich console (which corrupts the Textual display).
EmitFn = Callable[[str], None]

LEARNING_STATUSES = (
    "candidate",
    "approved",
    "applied",
    "rejected",
    "rollback_pending",
    "rolled_back",
    "failed",
)


def evolution_summary(counters: dict[str, int]) -> str:
    """Describe settings and current ledger state without claiming proposals are applied."""
    enabled = "on" if counters.get("learning_enabled") else "off"
    automatic = "on" if counters.get("auto_approve") else "off"
    return (
        f"Learning {enabled} · auto approval {automatic}\n"
        f"{counters.get('candidate', 0)} pending review · "
        f"{counters.get('approved', 0)} approved · {counters.get('applied', 0)} applied"
    )


def evolution_label(entry: dict[str, Any]) -> str:
    """Return the current decision state, or an explicitly historical label."""
    status = entry.get("status", "historical")
    if status == "historical":
        kind = {"unlock": "unlock", "levelup": "level-up"}.get(
            str(entry.get("kind", "")), "activity"
        )
        return f"Historical {kind} (application unverified)"
    return {
        "candidate": "Pending review",
        "approved": "Approved; awaiting application",
        "applied": "Applied",
        "rejected": "Rejected",
        "rollback_pending": "Rollback pending",
        "rolled_back": "Rolled back",
        "failed": "Application failed",
    }.get(status, str(status))


def evolution_detail(entry: dict[str, Any]) -> str:
    """Build literal report text shared by the console and native dashboard."""
    summary = str(entry.get("task_summary") or "No summary recorded.")
    if entry.get("source") != "ledger":
        return f"{evolution_label(entry)}\n{summary}"
    return (
        f"{entry['reference']} · {entry['kind']} · {evolution_label(entry)}\n"
        f"Scope: {entry['scope']} · Project: {entry['project'] or 'shared'}\n"
        "Use /learning review to inspect and manage proposals.\n\n" + summary
    )


def _learning_activity(
    agent_id: str = MAIN_AGENT_ID,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Read the current learning ledger and settings; never approve or apply anything."""
    from novacode_cli.config.config import Settings
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.hermes.ledger import LearningLedger

    cfg = NovaConfig()
    path = Settings.from_environment().get_agent_dir(agent_id) / "learning.sqlite3"
    counters = dict.fromkeys(LEARNING_STATUSES, 0)
    counters.update(
        learning_enabled=int(cfg.get_learning_enabled()),
        auto_approve=int(cfg.get_learning_auto_approve()),
    )
    entries = []
    for record, timestamp in LearningLedger.read_activity(path):
        counters[record.status] = counters.get(record.status, 0) + 1
        entries.append(
            {
                "source": "ledger",
                "status": record.status,
                "kind": record.kind,
                "skill": record.target or record.kind.replace("_", " ").title(),
                "reference": record.reference,
                "ts": timestamp,
                "task_summary": record.content[:8000],
                "scope": record.scope,
                "project": record.project,
                "origin": record.origin,
            }
        )
    return counters, entries


def _console_emit(message: str = "") -> None:
    """Default emitter (CLI/REPL): render a Rich-markup string to the console."""
    console.print(message)


async def _load_evolution(
    *, strict: bool = False, agent_id: str = MAIN_AGENT_ID
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Return current proposals followed by explicitly historical evolution records."""
    from novacode_cli.memory.store import get_durable_store

    store = await asyncio.to_thread(get_durable_store)

    try:
        counters, current = await asyncio.to_thread(_learning_activity, agent_id)
    except Exception:
        if strict:
            raise
        counters, current = {}, []
    counters.update(unlocked=0, leveled=0)
    try:
        entry = await store.aget(("nova", "meta"), "evolution")
        if entry and isinstance(entry.value, dict):
            counters.update(
                unlocked=int(entry.value.get("unlocked", 0)),
                leveled=int(entry.value.get("leveled", 0)),
            )
    except Exception:
        if strict:
            raise

    entries: list[dict[str, Any]] = []
    try:
        results = await store.asearch(("nova", "evolution_log"), limit=1000)
        for item in results or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                entries.append({**value, "source": "legacy", "status": "historical"})
    except Exception:
        if strict:
            raise
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return counters, [*current, *entries]


async def handle_evolution_command(
    limit: int = 15,
    emit: EmitFn | None = None,
    *,
    agent_id: str = MAIN_AGENT_ID,
) -> bool:
    """Render the evolution summary + recent unlocks/level-ups.

    Writes through ``emit`` (one Rich-markup line at a time) so the TUI can
    render natively; defaults to the Rich console for the CLI/REPL.
    """
    if emit is None:
        emit = _console_emit

    try:
        counters, entries = await _load_evolution(strict=True, agent_id=agent_id)
    except Exception as error:  # noqa: BLE001 — report storage failure without failing the task
        emit(f"Evolution history unavailable: {escape(str(error))}")
        return True

    emit("")
    emit(f"[bold {COLORS['primary']}]🧬 Self-Evolution[/bold {COLORS['primary']}]")
    emit(escape(evolution_summary(counters)))

    if not entries:
        emit(
            "  [dim]No activity yet. /learning on enables task-end reviews; "
            "/learning review lists proposals before application.[/dim]"
        )
        emit("")
        return True

    emit("")
    for e in entries[:limit]:
        icon = "•"
        verb = evolution_label(e)
        skill = e.get("skill", "?")
        when = ""
        ts = e.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            when = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")
        summary = (e.get("task_summary") or "").strip()
        line = f"  {icon} [bold]{escape(str(skill))}[/bold] [dim]{escape(verb)}"
        if when:
            line += f" · {when}"
        line += "[/dim]"
        emit(line)
        if summary:
            emit(escape(evolution_detail(e)))
    emit("")
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Register the read-only evolution report for console transports."""

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_evolution_command(agent_id=ctx.assistant_id)

    registry.register("evolution", _handle)
