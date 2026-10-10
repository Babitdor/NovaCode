"""On-disk sandbox registry — process-ownership + liveness for reclaim.

Cleanup of sandboxes was driven solely by the ``with create_sandbox(...)``
``finally`` block, so an abrupt end (SIGKILL / crash / terminal close) leaked the
sandbox, and ``/clear`` left a container pinned to the old session's immutable
Docker label. This registry is the source of truth for *which process owns which
sandbox for which session*, so:

- a heartbeat proves the owner is alive; the next startup can safely reclaim a
  still-**running** crash-orphan whose owner is dead, without touching a
  concurrent live session;
- ``/clear`` just re-ties the record to the new session id (no relabel needed);
- it is provider-agnostic, so cloud sandboxes (which had no sweep) are reclaimed
  too.

Storage: one JSON file per sandbox under ``~/.nova/sandboxes/<id>.json`` — each
process only ever writes its own files, so parallel Nova instances don't race.
The module is intentionally dependency-free; provider SDKs are lazy-imported
only inside the reclaim path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nova.sandbox.registry")

# No heartbeat within this ⇒ the owner is presumed gone (≈4× the 30s interval).
_HEARTBEAT_STALE_SECS = 120.0
# Regardless of pid liveness (which can be flaky/reused), a record this old is
# definitely abandoned and reclaimable.
_HARD_STALE_SECS = 1800.0
# How often the live process refreshes its heartbeat.
HEARTBEAT_INTERVAL_SECS = 30.0


def _registry_dir() -> Path:
    d = Path.home() / ".nova" / "sandboxes"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("Could not create sandbox registry dir", exc_info=True)
    return d


def _record_path(sandbox_id: str) -> Path:
    safe = "".join(c for c in str(sandbox_id) if c.isalnum() or c in "-_.")[:120]
    return _registry_dir() / f"{safe or 'unknown'}.json"


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe. True when alive OR indeterminate; False only
    when the pid is *definitely* gone (so a False can never kill a live owner)."""
    if not pid or pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by another user
        except OSError:
            return True  # indeterminate — treat as alive (conservative)
        return True
    # Windows
    try:
        import ctypes

        process_query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        still_active = 259  # STILL_ACTIVE
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query, False, int(pid))
        if not handle:
            return False  # no such process
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — can't probe ⇒ assume alive (conservative)
        return True


# ── Store ────────────────────────────────────────────────────────────────────


def register(
    sandbox_id: str,
    *,
    provider: str,
    session_id: str | None,
    persist: bool = False,
    pid: int | None = None,
) -> None:
    """Write/overwrite the record for ``sandbox_id`` (owned by this process)."""
    now = time.time()
    rec = {
        "sandbox_id": str(sandbox_id),
        "provider": provider,
        "session_id": session_id or "",
        "persist": bool(persist),
        "pid": int(pid if pid is not None else os.getpid()),
        "created_ts": now,
        "heartbeat_ts": now,
    }
    _write_record(sandbox_id, rec)


def heartbeat(sandbox_id: str) -> None:
    """Refresh the liveness timestamp (and pid) for a live sandbox."""
    rec = read_record(sandbox_id)
    if rec is None:
        return
    rec["heartbeat_ts"] = time.time()
    rec["pid"] = os.getpid()
    _write_record(sandbox_id, rec)


def retie(sandbox_id: str, new_session_id: str) -> None:
    """Re-own a live sandbox to a new session (used by ``/clear``)."""
    rec = read_record(sandbox_id)
    if rec is None:
        return
    rec["session_id"] = new_session_id or ""
    rec["heartbeat_ts"] = time.time()
    _write_record(sandbox_id, rec)


def deregister(sandbox_id: str) -> None:
    """Remove the record (clean exit / after reclaim)."""
    try:
        _record_path(sandbox_id).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove registry record %s", sandbox_id, exc_info=True)


def read_record(sandbox_id: str) -> dict[str, Any] | None:
    return _read_path(_record_path(sandbox_id))


def list_records() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for p in _registry_dir().glob("*.json"):
            rec = _read_path(p)
            if rec is not None:
                out.append(rec)
    except OSError:
        logger.debug("Could not list sandbox registry", exc_info=True)
    return out


def dead_records(
    *, now: float | None = None, exclude_pid: int | None = None
) -> list[dict[str, Any]]:
    """Records whose owning process is gone (reclaimable).

    Dead = (pid definitively not alive AND heartbeat stale) OR hard-stale. The
    AND on the first branch avoids reclaiming a live owner whose heartbeat thread
    merely stalled; the hard-stale branch backstops a flaky/reused pid probe.
    """
    now = now if now is not None else time.time()
    out: list[dict[str, Any]] = []
    for rec in list_records():
        pid = int(rec.get("pid", 0) or 0)
        if exclude_pid is not None and pid == exclude_pid:
            continue
        age = now - float(rec.get("heartbeat_ts", 0.0) or 0.0)
        stale = age > _HEARTBEAT_STALE_SECS
        if (not _pid_alive(pid) and stale) or age > _HARD_STALE_SECS:
            out.append(rec)
    return out


def _write_record(sandbox_id: str, rec: dict[str, Any]) -> None:
    path = _record_path(sandbox_id)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, path)  # atomic
    except OSError:
        logger.debug("Could not write registry record %s", sandbox_id, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_path(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── Reclaim (lazy provider imports) ──────────────────────────────────────────


def _saved_session_ids() -> set[str]:
    """Session ids that still exist on disk (mirrors sandbox_factory)."""
    try:
        return {p.name for p in (Path.home() / ".nova" / "sessions").iterdir() if p.is_dir()}
    except OSError:
        return set()


def _terminate_docker(sandbox_id: str, *, persist: bool, session_id: str) -> bool:
    import docker

    client = docker.from_env()
    try:
        cont = client.containers.get(sandbox_id)
    except docker.errors.NotFound:
        return True  # already gone — record can be dropped
    # A persisted container for a still-saved session is meant to survive for
    # resume: just ensure it's stopped (the dead owner left it running). Anything
    # else is reclaimed outright.
    if persist and session_id in _saved_session_ids():
        if getattr(cont, "status", "") == "running":
            cont.stop(timeout=10)
        return True
    try:
        cont.stop(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    cont.remove(force=True)
    return True


def _terminate_record(rec: dict[str, Any]) -> bool:
    """Terminate the sandbox for one dead record. Best-effort; never raises."""
    provider = rec.get("provider", "")
    sandbox_id = rec.get("sandbox_id", "")
    if not sandbox_id:
        return True
    try:
        if provider == "docker":
            return _terminate_docker(
                sandbox_id,
                persist=bool(rec.get("persist", False)),
                session_id=str(rec.get("session_id", "")),
            )
        if provider == "modal":
            import modal

            modal.Sandbox.from_id(sandbox_id).terminate()
            return True
        if provider == "runloop":
            from runloop_api_client import Runloop

            token = os.environ.get("RUNLOOP_API_KEY")
            if not token:
                return False
            Runloop(bearer_token=token).devboxes.shutdown(id=sandbox_id)
            return True
        if provider == "daytona":
            from daytona import Daytona, DaytonaConfig

            token = os.environ.get("DAYTONA_API_KEY")
            if not token:
                return False
            client = Daytona(DaytonaConfig(api_key=token))
            client.get(sandbox_id).delete()
            return True
        if provider == "langsmith":
            from langsmith.sandbox import SandboxClient

            token = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
            if not token:
                return False
            client = SandboxClient()
            sb = client.get_sandbox(sandbox_id)
            sb.delete()
            return True
    except Exception:  # noqa: BLE001
        logger.debug("Reclaim failed for %s/%s", provider, sandbox_id, exc_info=True)
        return False
    return False


def reclaim_dead_sandboxes(*, exclude_pid: int | None = None) -> list[str]:
    """Terminate sandboxes whose owning process is dead; drop their records.

    Best-effort and quiet — must never block startup. Returns the ids reclaimed.
    """
    if exclude_pid is None:
        exclude_pid = os.getpid()
    reclaimed: list[str] = []
    for rec in dead_records(exclude_pid=exclude_pid):
        if _terminate_record(rec):
            deregister(rec.get("sandbox_id", ""))
            reclaimed.append(str(rec.get("sandbox_id", "")))
    return reclaimed


def terminate_owned(*, pid: int | None = None) -> None:
    """At-exit net: terminate + deregister sandboxes owned by this process."""
    pid = pid if pid is not None else os.getpid()
    for rec in list_records():
        if int(rec.get("pid", 0) or 0) != pid:
            continue
        _terminate_record(rec)
        deregister(rec.get("sandbox_id", ""))
