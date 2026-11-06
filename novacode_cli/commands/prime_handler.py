"""Slash-command controls for session-scoped Nova Prime Deep Work."""

from __future__ import annotations

from contextlib import contextmanager
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

FACTORY_VERSION = 2
FACTORY_USAGE = (
    "Usage: /prime [on|off|pause|cancel|status|report|agents|sources|"
    "budget <tokens> <minutes>|waive <id> <reason>|tasks|checks [id]|resume <id>|forget <id>]"
)


def is_factory_report(report: object) -> bool:
    """Distinguish versioned factory records from legacy checkpoint reports."""
    return (
        isinstance(report, dict)
        and report.get("version") == FACTORY_VERSION
        and report.get("factory") is True
    )


def factory_receipt(report: dict[str, Any]) -> str:
    """Render host state without interpreting a worker summary as verification."""
    lines = [
        f"Prime Software Factory · {report.get('status', 'paused')} · "
        f"{report.get('stage', 'requirements')}",
        str(report.get("goal", "")),
        "Continuation: " + str(report.get("id", "")),
    ]
    budget, usage = report.get("budget", {}), report.get("usage", {})
    lines.append(
        f"Usage: {usage.get('tokens', 0)} / {budget.get('tokens', 500_000)} tokens; "
        f"{usage.get('active_seconds', 0):.0f} / "
        f"{budget.get('active_seconds', 3600):.0f} active seconds"
    )
    lines.extend(
        f"Task {task.get('id', '?')} · {task.get('role', '?')} · "
        f"{task.get('status', 'pending')} · needs {task.get('depends_on', [])}"
        for task in report.get("tasks", [])
    )
    for source in report.get("sources", []):
        content = source.get("content", source)
        lines.append(
            f"Source: {content.get('url', content.get('requested_url', '?'))} · "
            f"{content.get('dependency_version', content.get('version', 'unspecified version'))}"
        )
    lines.extend(
        f"Check {check.get('id', '?')}: {check.get('status', 'unverified')}"
        for check in report.get("checks", [])
    )
    approvals = report.get("approvals", {})
    if isinstance(approvals, dict):
        approvals = [{"kind": kind, "status": "approved"} for kind in approvals]
    lines.extend(
        f"Approval: {approval.get('kind', '?')} · {approval.get('status', 'pending')}"
        for approval in approvals
    )
    lines.extend("Blocked: " + str(item) for item in report.get("blockers", []))
    from novacode_cli.prime_factory.state import digest

    lines.extend(
        f"Finding {digest(finding)}: {finding}" for finding in report.get("review_findings", [])
    )
    lines.extend(
        f"Human waiver {key}: {waiver['reason']} (candidate {waiver['candidate']})"
        for key, waiver in report.get("waivers", {}).items()
    )
    if report.get("candidate"):
        lines.append("Candidate: " + str(report["candidate"]))
    lines.extend(
        f"Role {role}: {amount.get('tokens', 0)} tokens; "
        f"{amount.get('estimated_calls', 0)} calls with estimated usage"
        for role, amount in sorted(usage.get("by_role", {}).items())
    )
    lines.append(
        "Costs are unknown unless recorded by the provider; token counts are usage, not prices."
    )
    return "\n".join(lines)


def factory_records(session: Any, identity: str = "") -> list[dict[str, Any]]:  # noqa: ANN401
    """Read existing repository-local factory records without creating a store on inspection."""
    from novacode_cli.prime_factory.controller import storage_path
    from novacode_cli.prime_factory.store import FactoryStore

    path = storage_path(Path(session_directory(session)))
    if not path.is_file():
        return []
    store = FactoryStore(path)
    try:
        return [store.load(identity)] if identity else store.records()
    finally:
        store.close()


@contextmanager
def _factory_control(session: Any):  # noqa: ANN401, ANN202 - Sync command/session boundary.
    """Control a saved paused run without requiring it to spend another model call."""
    controller = getattr(session, "prime_factory", None)
    if controller is not None:
        yield controller
        return
    from novacode_cli.prime_factory.controller import FactoryController, storage_path
    from novacode_cli.prime_factory.store import FactoryStore

    report = getattr(session, "prime_report", None)
    if not is_factory_report(report):
        yield None
        return
    store = FactoryStore(storage_path(Path(session_directory(session))))
    try:
        record = store.load(report["id"])
        record.setdefault("origin_thread", record["thread"])
        record["thread"] = session.thread_id
        controller = FactoryController(store, record, session)
        yield controller
    finally:
        store.close()


def _stop_saved_factory(control: Any) -> None:  # noqa: ANN401 - Durable controller boundary.
    """Reconcile saved workers on a command thread without resuming model work."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from novacode_cli.prime_factory.controller import ACTIVE
    from novacode_cli.teams.recovery import RecoveryHandle

    for attempt in control.record["attempts"]:
        if attempt["status"] in ACTIVE:
            recovery = attempt.get("recovery") or {}
            if recovery.get("kind") == "factory_start":
                from novacode_cli.prime_factory.recovery import reconcile_start

                with ThreadPoolExecutor(max_workers=1) as pool:
                    stopped = pool.submit(
                        lambda record=recovery: asyncio.run(reconcile_start(record))
                    ).result()
                attempt["status"] = "stopped" if stopped else "uncertain"
            else:
                control.handles[attempt["id"]] = RecoveryHandle(recovery)
    if control.handles:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(control.stop_workers())).result()
    control.save()


def _factory_command(  # noqa: PLR0911, PLR0912, PLR0915 - Explicit command grammar.
    session: Any,  # noqa: ANN401 - Session proxies.
    parts: list[str],
) -> str | None:
    """Apply explicit host controls, never approve work through a slash command."""
    action = parts[0].lower() if parts else ""
    report = getattr(session, "prime_report", None)
    factory = is_factory_report(report)
    controller = getattr(session, "prime_factory", None)
    disabling = action == "off" or (not action and getattr(session, "prime_enabled", False))
    if disabling and len(parts) <= 1 and factory:
        with _factory_control(session) as control:
            control.request_pause()
            if controller is None:
                _stop_saved_factory(control)
    if action == "resume" and len(parts) in {1, 2}:
        identity = parts[1] if len(parts) > 1 else report["id"] if factory else ""
        record = (
            report
            if factory and identity == report["id"]
            else next((row for row in factory_records(session) if row["id"] == identity), None)
        )
        if record is not None:
            session.prime_factory_resume = identity
            session.prime_enabled = True
            session.prime_report = {
                **deepcopy(record),
                "thread": session.thread_id,
                "status": "paused",
            }
            return (
                f"Factory {identity} prepared for explicit resume. "
                "Send a message to continue; approvals are revalidated."
            )
    if action in {"pause", "cancel"}:
        if len(parts) != 1:
            return f"Usage: /prime {action}"
        with _factory_control(session) as control:
            if control is None:
                return "No factory run in this session."
            if action == "pause":
                control.request_pause()
            else:
                control.request_cancel()
            if controller is None:
                _stop_saved_factory(control)
        return (
            f"Factory {action} requested. "
            "Owned workers will stop; unfinished branches are preserved."
        )
    if action == "budget":
        if len(parts) != 3:  # noqa: PLR2004 - Command plus two budget values.
            return "Usage: /prime budget <tokens> <active-minutes>"
        try:
            tokens, minutes = int(parts[1]), float(parts[2])
        except ValueError:
            return "Budget requires positive tokens and finite active minutes."
        if tokens <= 0 or not 0 < minutes < float("inf"):
            return "Budget requires positive tokens and finite active minutes."
        with _factory_control(session) as control:
            if control is None:
                return "No factory run in this session."
            control.set_budget(tokens, minutes * 60)
        return f"Factory budget set to {tokens} tokens and {minutes:g} active minutes."
    if action == "waive":
        if len(parts) < 3:  # noqa: PLR2004 - Command, target ID and specific reason.
            return "Usage: /prime waive <check-or-finding-id> <specific-reason>"
        with _factory_control(session) as control:
            if control is None:
                return "No factory run in this session."
            control.waive(parts[1], " ".join(parts[2:]))
        return (
            "Human waiver recorded for this candidate. Resume to revalidate; "
            "delivery approval remains required."
        )
    if action in {"agents", "sources"}:
        if len(parts) != 1:
            return f"Usage: /prime {action}"
        if not factory:
            return "No factory run in this session."
        if controller is not None:
            report = controller.record
        else:
            report = next(iter(factory_records(session, report["id"])), report)
        records = report.get("sources", [])
        if action == "agents":
            records = {
                "tasks": report.get("tasks", []),
                "attempts": report.get("attempts", []),
                "routes": report.get("routes", {}),
                "usage_by_role": report.get("usage", {}).get("by_role", {}),
            }
        import json

        return json.dumps(records, ensure_ascii=False, indent=2)
    if factory and action in {"status", "report"} and len(parts) == 1:
        if action == "report":
            report = (
                controller.record
                if controller is not None
                else next(
                    iter(factory_records(session, report["id"])),
                    report,
                )
            )
        return factory_receipt(report)
    if action == "tasks" and len(parts) == 1:
        records = factory_records(session)
        if records:
            memory = PrimeMemory(project_context(session_directory(session)))
            lines = [f"{row['id']} · factory · {row['status']} · {row['goal']}" for row in records]
            return "\n".join(lines) + "\n" + _inspect(memory, parts)
    if action == "checks" and len(parts) <= 2:  # noqa: PLR2004 - Command and optional ID.
        records = factory_records(session)
        checks = [(row, check) for row in records for check in row.get("checks", [])]
        if len(parts) == 2:  # noqa: PLR2004 - Command and ID.
            import json

            match = next(((row, check) for row, check in checks if check["id"] == parts[1]), None)
            if match is not None:
                row, _ = match
                row = next(iter(factory_records(session, row["id"])), row)
                check = next(check for check in row["checks"] if check["id"] == parts[1])
                return json.dumps(
                    {"run_id": row["id"], "check": check}, ensure_ascii=False, indent=2
                )
        elif checks:
            return "\n".join(
                f"{check['id']} · {check['status']} · {check['purpose']} · run {row['id']}"
                for row, check in checks
            )
    return None


def handle_prime_command(  # noqa: PLR0911 - Legacy and factory command dispatch.
    session: Any,  # noqa: ANN401 - Session proxies.
    args: str = "",
) -> str:
    """Toggle the next request's workflow without starting work or changing permissions."""
    parts = args.strip().split()
    action = parts[0].lower() if parts else ""
    try:
        result = _factory_command(session, parts)
    except (OSError, ValueError) as exc:
        return "Factory control failed: " + str(exc)
    if result is not None:
        return result
    if action in {"tasks", "checks", "resume", "forget"}:
        try:
            return _records_command(session, parts)
        except Exception:  # noqa: BLE001 - Storage errors are local status, never an agent request.
            return "Prime storage unavailable. Your workspace and conversation are unchanged."
    if action not in {"", "on", "off", "status", "report"}:
        return FACTORY_USAGE
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
    message = f"Nova Prime · Software Factory: {mode}."
    if action in {"", "on", "off"}:
        message += (
            " Applies to the next request; existing models, limits and approvals stay in force."
        )
    if report:
        if is_factory_report(report):
            return message + f"\nFactory: {report['status']} · {report['stage']}."
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
        import asyncio

        result = await asyncio.to_thread(
            handle_prime_command, ctx.session_state, ctx.cmd_args or ""
        )
        console.print(result, markup=False)
        return True

    registry.register("prime", handler)
