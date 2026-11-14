"""Session-scoped scratchpad: per-session temp dir for intermediate agent work.

Provisioned on demand, mounted at /scratchpad/ in the file backend, auto-approved
for file operations inside it, and removed when the session ends. A stale sweep
cleans directories left behind by crashed sessions.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import psutil

logger = logging.getLogger(__name__)

SCRATCHPAD_VIRTUAL_PREFIX = "/scratchpad/"
_PREFIX = "nova-scratchpad-"
_DEFAULT_MAX_AGE_HOURS = 24

_lock = RLock()
# session_id -> resolved host path
_active: dict[str, Path] = {}
_atexit_registered = False


@dataclass(frozen=True)
class ScratchpadInfo:
    """One session's virtual mount and private host directory."""

    session_id: str
    virtual_prefix: str
    host_path: Path


def _safe_session_id(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-._")
    return cleaned or "default"


def provision(session_id: str | None) -> ScratchpadInfo | None:
    """Create (idempotently) and register the scratchpad for *session_id*."""
    if not session_id:
        return None
    with _lock:
        existing = get(session_id)
        if existing is not None:
            return existing
        try:
            host = Path(
                tempfile.mkdtemp(prefix=f"{_PREFIX}{_safe_session_id(session_id)[:48]}-")
            ).resolve()
            (host / ".nova-scratchpad").write_text(
                json.dumps({"session_id": session_id, "pid": os.getpid()}), encoding="utf-8"
            )
        except OSError:
            logger.debug("Could not create scratchpad for session %s", session_id, exc_info=True)
            return None
        _active[session_id] = host
        _register_atexit_locked()
    return ScratchpadInfo(
        session_id=session_id, virtual_prefix=SCRATCHPAD_VIRTUAL_PREFIX, host_path=host
    )


def get(session_id: str | None) -> ScratchpadInfo | None:
    """Look up an existing scratchpad without creating it."""
    if not session_id:
        return None
    with _lock:
        host = _active.get(session_id)
    if host is None or not host.is_dir():
        return None
    return ScratchpadInfo(
        session_id=session_id, virtual_prefix=SCRATCHPAD_VIRTUAL_PREFIX, host_path=host
    )


def is_under_scratchpad(path: str | os.PathLike[str] | Path) -> bool:
    """True only inside the calling session's active scratchpad."""
    from novacode_cli.config.plan_mode import active_plan_session

    info = get(getattr(active_plan_session.get(), "session_id", None))
    if info is None:
        return False
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved.is_relative_to(info.host_path)


def cleanup(session_id: str | None) -> None:
    """Remove and unregister the scratchpad for *session_id*."""
    if not session_id:
        return
    with _lock:
        host = _active.pop(session_id, None)
    if host is None:
        return
    # Only remove directories that carry our marker/prefix.
    if not _is_owned_scratchpad(host):
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(host, ignore_errors=True)


def _is_owned_scratchpad(path: Path) -> bool:
    name = path.name
    if not name.startswith(_PREFIX):
        return False
    try:
        return (
            path.resolve() == path.absolute()
            and path.parent.resolve() == Path(tempfile.gettempdir()).resolve()
            and (path / ".nova-scratchpad").is_file()
        )
    except (OSError, ValueError, RuntimeError):
        return False


def sweep_stale(max_age_hours: int = _DEFAULT_MAX_AGE_HOURS) -> int:
    """Remove stale nova-scratchpad-* dirs left by crashed sessions."""
    base = Path(tempfile.gettempdir())
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for entry in entries:
        try:
            if not entry.is_dir() or not _is_owned_scratchpad(entry):
                continue
            owner = json.loads((entry / ".nova-scratchpad").read_text(encoding="utf-8"))
            if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
                continue
            if psutil.pid_exists(owner["pid"]):
                continue
            # Keep dirs for sessions still active in this process.
            with _lock:
                if entry.resolve() in {p.resolve() for p in _active.values()}:
                    continue
            mtime = entry.stat().st_mtime
            if mtime > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed += 1
        except (OSError, ValueError):
            continue
    return removed


def _cleanup_all() -> None:
    with _lock:
        ids = list(_active)
    for sid in ids:
        cleanup(sid)


def _register_atexit_locked() -> None:
    global _atexit_registered  # noqa: PLW0603 -- One process-wide exit hook.
    if _atexit_registered:
        return
    atexit.register(_cleanup_all)
    _atexit_registered = True
