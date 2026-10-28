"""Local handoff controls shared by TUI, console and remote command dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novacode_cli.handoff import HandoffMemory, document, prepare
from novacode_cli.prime_memory import session_directory

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry


def handle_handoff_command(session: Any, args: str = "") -> str:  # noqa: ANN401
    """Toggle or inspect context without starting work or restoring approval decisions."""
    parts = args.strip().split()
    action = parts[0].lower() if parts else "list"
    if action in {"on", "off", "status"} and len(parts) == 1:
        if action != "status":
            session.handoff_enabled = action == "on"
        state = "on" if getattr(session, "handoff_enabled", False) is True else "off"
        return f"Handoff mode: {state}. Saves project context during subsequent requests."
    try:
        if action == "list" and len(parts) <= 1:
            records = HandoffMemory(session_directory(session)).records()
            return "\n".join(f"{r['id']} · {r['title']} · {r['status']}" for r in records) or (
                "No saved handoffs in this project. "
                "Enable Handoff beside the composer, then send a task."
            )
        if action == "preview" and len(parts) == 2:  # noqa: PLR2004 - Command grammar.
            record = HandoffMemory(session_directory(session)).get(parts[1])
            return document(record) if record else "Handoff not found in this project."
        if action == "resume" and len(parts) == 2:  # noqa: PLR2004 - Command grammar.
            return prepare(session, parts[1])
    except Exception:  # noqa: BLE001 - Storage errors remain temporary local notices.
        return "Handoff storage unavailable. Your workspace and conversation are unchanged."
    return (
        "Usage: /handoff [on|off|status|list|preview <id>|resume <id>] "
        "· /skill:handoff invokes the skill."
    )


def register_commands(registry: CommandRegistry) -> None:
    """Make handoff commands available through the canonical registry."""
    from novacode_cli.config.config import console

    async def handler(ctx: CommandContext) -> bool:
        console.print(handle_handoff_command(ctx.session_state, ctx.cmd_args or ""), markup=False)
        return True

    registry.register("handoff", handler)
