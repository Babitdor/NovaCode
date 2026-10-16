"""Handler for the ``/refine`` command.

Subcommands:
- ``/refine`` (no args) → run the refinement loop (plan → apply → review)
- ``/refine plan`` → dry-run plan of proposed refinements
- ``/refine status`` → harness state (skills, prompt overrides, memory topics)
- ``/refine history`` → unified refinement audit trail
- ``/refine rollback <event_id>`` → revert a recorded refinement
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.table import Table

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from novacode_cli.commands import CommandContext, CommandRegistry


def _nova_root() -> Path:
    """Resolve the shared ``~/.nova`` root from the prompt-history location."""
    from novacode_cli.hermes.prompt_evolution import PROMPT_HISTORY_DIR

    return PROMPT_HISTORY_DIR.parent


def _user_skills_dir() -> Path:
    """Lazy alias so the module attribute exists for patching/tests."""
    from novacode_cli.hermes.refine_loop import _user_skills_dir as _impl

    return _impl()


def _store_from(session_state: object) -> object | None:
    """Extract the durable store from a session state (may be None)."""
    if session_state is None:
        return None
    return getattr(session_state, "_store", None)


def _read_events(nova_root: Path) -> list[dict]:
    path = nova_root / "refinement_events.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        return []
    return []


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "?"


async def _run_loop(store: object, console: Console) -> bool:
    """Run the refinement loop and print its summary."""
    if store is None:
        console.print("[yellow]No session store available; skipping /refine.[/yellow]")
        return True
    from novacode_cli.hermes.refine_loop import run_refine
    from novacode_cli.hermes.tracker import ToolUsageTracker

    summary = await run_refine(store, ToolUsageTracker(store))
    console.print(
        f"[cyan]/refine[/cyan] planned [bold]{summary.get('planned', 0)}[/bold], "
        f"proposed [bold]{summary.get('accepted', 0)}[/bold], "
        f"rolled back [bold]{summary.get('rolled_back', 0)}[/bold]."
    )
    for item in summary.get("items", []):
        console.print(
            f"  • {item.get('domain', '?')} / {item.get('target', '?')} "
            f"({item.get('action', '?')}) → {item.get('outcome', 'applied')}"
        )
    return True


async def _plan(store: object, console: Console) -> bool:
    """Dry-run the refinement planner and print proposed items."""
    if store is None:
        console.print("[yellow]No session store available; skipping /refine plan.[/yellow]")
        return True
    from novacode_cli.hermes.refine_loop import plan_refinements
    from novacode_cli.hermes.tracker import ToolUsageTracker

    plan = await plan_refinements(store, ToolUsageTracker(store))
    if not plan:
        console.print("[green]No refinements proposed.[/green]")
        return True
    console.print("[cyan]Proposed refinements:[/cyan]")
    for item in plan:
        console.print(
            f"  • {item.get('domain', '?')} / {item.get('target', '?')} "
            f"({item.get('action', '?')}) — {item.get('reason', '')}"
        )
    return True


async def _status(_store: object, console: Console) -> bool:
    """Print harness state: skills, prompt overrides, memory topics."""
    from novacode_cli.hermes import refine_loop

    skills = refine_loop._list_skills(_user_skills_dir())
    prompts = refine_loop._prompt_state()
    memory = refine_loop._memory_topics()
    table = Table(title="Refinement Harness Status")
    table.add_column("Area", style="cyan")
    table.add_column("Count", style="magenta")
    table.add_column("Details")
    table.add_row("Skills", str(len(skills)), ", ".join(s["name"] for s in skills[:8]))
    table.add_row("Prompt overrides", str(len(prompts)), f"{len(prompts)} template(s)")
    table.add_row("Memory topics", str(len(memory)), ", ".join(memory[:8]))
    console.print(table)
    return True


async def _history(_store: object, console: Console) -> bool:
    """Print the unified refinement audit trail."""
    events = _read_events(_nova_root())
    if not events:
        console.print(
            "[yellow]No refinement events recorded yet. "
            "The audit trail fills in as you evolve prompts, create/refine "
            "skills, and consolidate memory.[/yellow]"
        )
        return True

    table = Table(title="Refinement History (unified audit trail)")
    table.add_column("When", style="dim")
    table.add_column("Domain", style="cyan")
    table.add_column("Action", style="magenta")
    table.add_column("Target")
    table.add_column("Outcome", style="green")
    for ev in reversed(events[-50:]):
        table.add_row(
            _fmt_ts(ev.get("ts", 0)),
            str(ev.get("domain", "?")),
            str(ev.get("action", "?")),
            str(ev.get("target", "")),
            str(ev.get("outcome", "applied")),
        )
    console.print(table)
    return True


async def _rollback(args: list[str], console: Console) -> bool:
    """Revert a recorded refinement by event id."""
    event_id = args[1] if len(args) > 1 else ""
    if not event_id:
        console.print("[red]Usage: /refine rollback <event_id>[/red]")
        return True
    from novacode_cli.hermes.refinement_log import rollback_refinement

    ok, message = rollback_refinement(_nova_root(), event_id)
    if ok:
        console.print(f"[green]{message}[/green]")
    else:
        console.print(f"[yellow]{message}[/yellow]")
    return True


async def handle_refine_command(
    cmd_args: str | None,
    session_state: object,
    console: Console,
) -> bool:
    """Handle ``/refine`` subcommands (run, plan, status, history, rollback)."""
    args = (cmd_args or "").strip().split()
    sub = args[0].lower() if args else "run"
    store = _store_from(session_state)
    handlers = {
        "run": _run_loop,
        "plan": _plan,
        "status": _status,
        "history": _history,
        "rollback": _rollback,
    }
    handler = handlers.get(sub)
    if handler is None:
        console.print(
            f"[red]Unknown /refine subcommand '{sub}'. "
            "Supported: run, plan, status, history, rollback <event_id>.[/red]"
        )
        return True
    if sub == "rollback":
        return await _rollback(args, console)
    return await handler(store, console)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_commands(registry: CommandRegistry) -> None:
    """Register the ``/refine`` command with the given registry."""

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_refine_command(ctx.cmd_args, ctx.session_state, ctx.console)

    registry.register("refine", _handle)
