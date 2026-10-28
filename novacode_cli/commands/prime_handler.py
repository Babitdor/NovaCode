"""Slash-command controls for session-scoped Nova Prime Deep Work."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.prime import PHASES, prime_payload, receipt
from novacode_cli.prime_memory import (
    PrimeMemory,
    continuation_context,
    freshness,
    project_context,
    session_directory,
)

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry


def handle_prime_command(session: Any, args: str = "") -> str:  # noqa: ANN401 - Session proxies.
    """Toggle the next request's workflow without starting work or changing permissions."""
    parts = args.strip().split()
    action = parts[0].lower() if parts else ""
    if action in {"tasks", "checks", "resume", "forget"}:
        try:
            return _records_command(session, parts)
        except Exception:  # noqa: BLE001 - Storage errors are local status, never an agent request.
            return "Prime storage unavailable. Your workspace and conversation are unchanged."
    if action not in {"", "on", "off", "status", "report"}:
        return "Usage: /prime [on|off|status|report|tasks|checks [id]|resume <id>|forget <id>]"
    if len(parts) > 1:
        return "Usage: /prime [on|off|status|report|tasks|checks [id]|resume <id>|forget <id>]"
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


def _records_command(session: Any, parts: list[str]) -> str:  # noqa: ANN401 - Session boundary.
    """Inspect or prepare saved context; no execution, workspace switching or approval restore."""
    context = project_context(session_directory(session))
    memory = PrimeMemory(context)
    action = parts[0].lower()
    if action in {"tasks", "checks"}:
        return _inspect(memory, parts)
    if len(parts) < 2 or len(parts) > 3:  # noqa: PLR2004 - Command grammar.
        return f"Usage: /prime {action} <id>"
    identity = parts[1]
    if action == "resume":
        return _prepare(session, memory, identity)
    return _forget(session, memory, parts)


def _inspect(memory: PrimeMemory, parts: list[str]) -> str:
    """Read definitions and captured evidence on explicit inspection."""
    import json

    action = parts[0].lower()
    if len(parts) > 1 and action == "checks":
        check = memory.get("checks", parts[1])
        if check is None:
            return "Prime check not found in this project."
        evidence = [memory.get("evidence", key) for key in check.get("evidence_ids", [])]
        return json.dumps({"check": check, "evidence": evidence}, ensure_ascii=False, indent=2)
    lines = []
    for record in memory.records(action):
        label = str(record.get("goal") or record.get("purpose") or "")[:150]
        state = freshness(memory.context["root"], record.get("fingerprints", {}))
        status = record.get("status", "draft")
        if status == "running":
            status = "paused (saved snapshot)"
        lines.append(
            f"{record['id']} · {status} · {record.get('finding', 'draft')} · {state} · {label}"
        )
    return "\n".join(lines) or f"No saved Prime {action} in this project."


def _prepare(session: Any, memory: PrimeMemory, identity: str) -> str:  # noqa: ANN401
    """Load context, retaining the current workspace, model and policy."""
    report = memory.get("tasks", identity)
    if report is None:
        return "Prime task not found in this project."
    context = memory.context
    warnings = []
    if report.get("cwd") != context["cwd"]:
        warnings.append("Workspace differs; continuation uses the current workspace.")
    if not Path(report.get("cwd", "")).is_dir():
        warnings.append("Saved worktree is missing.")
    if report.get("revision") != context["revision"]:
        warnings.append("Revision changed.")
    state = freshness(context["root"], report.get("fingerprints", {}))
    if state != "current":
        warnings.append(f"Files or dependencies: {state}.")
    session.prime_pending = deepcopy(continuation_context(report))
    session.prime_enabled = True
    session.prime_report = {**deepcopy(report), "thread": session.thread_id, "status": "paused"}
    return (
        f"Prime task {identity} prepared. "
        + " ".join(warnings)
        + " Send another message or choose Continue to begin. "
        "Current model, limits and permissions apply."
    )


def _forget(session: Any, memory: PrimeMemory, parts: list[str]) -> str:  # noqa: ANN401
    """Require a pending local confirmation before removing a saved task or finding."""
    identity = parts[1]
    kind = next(
        (name for name in ("tasks", "checks") if memory.get(name, identity) is not None),
        None,
    )
    if kind is None:
        return "Prime record not found in this project."
    pending_delete = getattr(session, "prime_forget_pending", None)
    target = memory.context["project"] + ":" + identity
    if parts[-1] != "confirm" or pending_delete != target:
        session.prime_forget_pending = target
        return (
            f"Confirm local removal with /prime forget {identity} confirm. "
            "Repository files and conversation history stay intact."
        )
    if kind == "checks":
        check = memory.get(kind, identity) or {}
        for key in check.get("evidence_ids", []):
            memory.forget("evidence", key)
    memory.forget(kind, identity)
    session.prime_forget_pending = None
    if (getattr(session, "prime_pending", None) or {}).get("id") == identity:
        session.prime_pending = None
    return f"Prime record {identity} removed."


def register_commands(registry: CommandRegistry) -> None:
    """Register the same semantics for command and remote consumers."""
    from novacode_cli.config.config import console

    async def handler(ctx: CommandContext) -> bool:
        console.print(handle_prime_command(ctx.session_state, ctx.cmd_args or ""), markup=False)
        return True

    registry.register("prime", handler)
