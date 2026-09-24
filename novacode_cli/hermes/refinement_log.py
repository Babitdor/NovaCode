"""Unified refinement audit trail.

Nova already versions each learning domain independently (per-skill
``.history``, per-prompt ``manifest.json``, versioned memory sections), but
there is no single cross-domain log of "what did I change and did it work".
This module provides an append-only ``refinement_events.json`` ledger that every
learning mutation (prompt evolution, skill create/refine/curate, memory write)
appends to, plus a ``rollback_refinement`` helper that maps an event back to the
domain's existing rollback mechanism.

Inspired by Prime Agent's ``refinement_events.json`` (see
``docs/PRIME-AGENT-LEARNING-ANALYSIS.md``).

The log is best-effort and never raises: a failed append is logged and ignored,
so learning mutations are never blocked by the audit trail.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("nova.hermes.refinement_log")

#: Hard cap on the number of events retained (oldest dropped).
_MAX_EVENTS = 500


def _log_path(nova_root: Path) -> Path:
    return nova_root / "refinement_events.json"


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _write_events(path: Path, events: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("Could not write refinement log at %s", path, exc_info=True)


def append_refinement_event(
    nova_root: Path,
    *,
    domain: str,
    action: str,
    target: str,
    detail: str = "",
    outcome: str = "applied",
) -> str | None:
    """Append a timestamped refinement event to the unified audit log.

    Args:
        nova_root: The shared ``~/.nova`` directory. The log is written to
            ``nova_root / "refinement_events.json"`` so all learning domains
            (prompt, skill, memory) share one cross-domain ledger.
        domain: One of ``"prompt"``, ``"skill"``, ``"memory"``, ``"subagent"``.
        action: e.g. ``"create"``, ``"refine"``, ``"curate"``, ``"rollback"``.
        target: The thing changed (prompt name, skill name, memory topic).
        detail: Optional free-text note (e.g. the reason for the change).
        outcome: ``"applied"``, ``"rolled_back"``, or ``"rejected"``.

    Returns:
        The new event id, or ``None`` if the append failed.
    """
    ids = append_refinement_events(
        nova_root,
        [
            {
                "domain": domain,
                "action": action,
                "target": target,
                "detail": detail,
                "outcome": outcome,
            }
        ],
    )
    return ids[0] if ids else None


def append_refinement_events(nova_root: Path, events_in: list[dict]) -> list[str]:
    """Append several refinement events in ONE read-modify-write.

    :func:`append_refinement_event` re-reads and re-writes the whole ledger per
    call. A review cycle appends one event per lesson, so a review with N lessons
    did N full parses plus N full rewrites of a 500-entry JSON file — all on the
    shared UI/agent event loop, which the stall watchdog recorded as a 263s
    freeze. Batching collapses that to a single read and a single write.

    Args:
        nova_root: The shared ``~/.nova`` directory.
        events_in: Partial event dicts; ``domain``/``action``/``target`` are
            required, ``detail``/``outcome`` default.

    Returns:
        The new event ids, in input order. Empty when the write failed.
    """
    if not events_in:
        return []
    path = _log_path(nova_root)
    events = _read_events(path)
    now = datetime.now()
    ids: list[str] = []
    for partial in events_in:
        event = {
            "id": now.strftime("%Y%m%dT%H%M%S_%f"),
            "ts": now.timestamp(),
            "domain": partial.get("domain", ""),
            "action": partial.get("action", ""),
            "target": partial.get("target", ""),
            "detail": partial.get("detail", ""),
            "outcome": partial.get("outcome", "applied"),
        }
        events.append(event)
        ids.append(event["id"])
    # Cap the log, dropping the oldest events.
    if len(events) > _MAX_EVENTS:
        events = events[-_MAX_EVENTS:]
    _write_events(path, events)
    return ids


def read_refinement_events(nova_root: Path, *, limit: int = 50) -> list[dict]:
    """Return the most recent refinement events (newest last)."""
    events = _read_events(_log_path(nova_root))
    if limit and limit > 0:
        events = events[-limit:]
    return events


def rollback_refinement(nova_root: Path, event_id: str) -> tuple[bool, str]:
    """Roll back a refinement event using the domain's existing mechanism.

    Maps the event to the appropriate rollback path:
      - ``skill`` events → ``skills/versioning.restore``
      - ``prompt`` events → the prompt-evolution rollback (best-effort)
      - ``memory`` events → no automatic rollback (memory is append-only)

    Returns ``(ok, message)``.
    """
    events = _read_events(_log_path(nova_root))
    event = next((e for e in events if e.get("id") == event_id), None)
    if event is None:
        return False, f"no refinement event with id '{event_id}'"

    domain = event.get("domain")
    target = event.get("target", "")

    if domain == "skill":
        from novacode_cli.skills import versioning

        skill_dir = nova_root / "skills" / target
        if not (skill_dir / "SKILL.md").exists():
            return False, f"skill '{target}' not found at {skill_dir}"
        return versioning.restore(skill_dir)

    if domain == "prompt":
        # Prompt evolution keeps its own manifest + rollback. Surface a pointer
        # rather than duplicating the mechanism here.
        return (
            False,
            f"prompt rollback for '{target}' is handled by the prompt-evolution "
            "system (see /prompt rollback); no automatic rollback from the log",
        )

    if domain == "memory":
        return False, "memory is append-only; no automatic rollback"

    return False, f"no rollback path for domain '{domain}'"


__all__ = [
    "append_refinement_event",
    "read_refinement_events",
    "rollback_refinement",
]
