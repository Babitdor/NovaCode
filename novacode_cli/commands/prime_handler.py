"""Slash-command controls for session-scoped Nova Prime Deep Work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novacode_cli.prime import PHASES, prime_payload, receipt

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry


def handle_prime_command(session: Any, args: str = "") -> str:  # noqa: ANN401 - Session proxies.
    """Toggle the next request's workflow without starting work or changing permissions."""
    action = args.strip().lower()
    if action not in {"", "on", "off", "status", "report"}:
        return "Usage: /prime [on|off|status|report]"
    if action in {"", "on", "off"}:
        session.prime_enabled = (
            not getattr(session, "prime_enabled", False) if not action else action == "on"
        )
    payload = prime_payload(session) or {"enabled": False, "report": None}
    report = payload["report"]
    if action == "report":
        return (
            receipt(report)
            if report
            else "No Prime delivery record yet. Enable /prime on, then send a task."
        )
    mode = "on" if payload["enabled"] else "off"
    message = f"Nova Prime · Deep Work: {mode}."
    if action in {"", "on", "off"}:
        message += (
            " Applies to the next request; existing models, limits and approvals stay in force."
        )
    if report:
        message += (
            f"\nLast request: {report['status']} · {report['phase']} · "
            f"{len(report['checkpoints'])}/{len(PHASES)} checkpoints."
        )
    return message


def register_commands(registry: CommandRegistry) -> None:
    """Register the same semantics for command and remote consumers."""
    from novacode_cli.config.config import console

    async def handler(ctx: CommandContext) -> bool:
        console.print(handle_prime_command(ctx.session_state, ctx.cmd_args or ""), markup=False)
        return True

    registry.register("prime", handler)
