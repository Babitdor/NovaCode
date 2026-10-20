"""When Nova may dream: the three gates in front of automatic memory consolidation.

A dream (``/dream``) consolidates the memory wiki. Run automatically, it must
neither over-dream (burning tokens on a wiki that barely changed) nor
under-dream (letting a busy week's lessons sit unconsolidated). It runs only
when all three gates pass:

- **Time gate:** at least :data:`TIME_GATE` since the last dream.
- **Session gate:** at least :data:`SESSION_GATE` sessions (with messages)
  started since the last dream.
- **Lock gate:** the consolidation lock is free, so two Nova processes (or a
  manual ``/dream`` and an automatic one) never consolidate at once.

A manual ``/dream`` skips the time and session gates (the user asked) but still
takes the lock, and any finished dream resets both counters.

State lives beside the memory it guards: ``<agent_dir>/.dream.json`` and the
lock file ``<agent_dir>/.dream.lock``.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Minimum time between dreams.
TIME_GATE = timedelta(hours=24)
#: Minimum sessions started since the last dream.
SESSION_GATE = 5
#: A lock older than this belongs to a dream that died; it may be taken over.
STALE_LOCK = timedelta(hours=2)

_STATE = ".dream.json"
_LOCK = ".dream.lock"


def _now() -> datetime:
    return datetime.now(UTC)


def _read_state(agent_dir: Path) -> dict:
    try:
        return json.loads((agent_dir / _STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(agent_dir: Path, state: dict) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    tmp = agent_dir / f"{_STATE}.tmp"
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(agent_dir / _STATE)


def last_dream(agent_dir: Path) -> datetime:
    """When the gates were last reset.

    On the first check there is no record; that moment becomes the baseline, so
    upgrading Nova does not set off a dream over months of old sessions at once.
    """
    state = _read_state(agent_dir)
    try:
        return datetime.fromisoformat(state["last_dream_at"])
    except (KeyError, TypeError, ValueError):
        now = _now()
        _write_state(agent_dir, {**state, "last_dream_at": now.isoformat(), "trigger": "baseline"})
        return now


def record_dream(agent_dir: Path, trigger: str, outcome: str) -> None:
    """Reset the time and session gates after a dream finished."""
    state = _read_state(agent_dir)
    state.update(last_dream_at=_now().isoformat(), trigger=trigger, outcome=outcome)
    _write_state(agent_dir, state)


def sessions_since(sessions_dir: Path, since: datetime) -> int:
    """Sessions with at least one message started after ``since``."""
    cutoff = since.timestamp()
    count = 0
    try:
        entries = list(sessions_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            # A session untouched since the cutoff cannot have started after it;
            # this skips reading most of a large history's meta files.
            if not entry.is_dir() or entry.stat().st_mtime < cutoff:
                continue
            meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            started = datetime.fromisoformat(meta["created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if started > since and int(meta.get("message_count") or 0) > 0:
            count += 1
    return count


@dataclass(frozen=True)
class GateReport:
    """Each gate's state, for the decision and for ``/dream status``."""

    since: datetime
    sessions: int
    lock_holder: str | None  # "pid 1234 since 10:02", or None when free

    @property
    def time_ok(self) -> bool:
        """At least :data:`TIME_GATE` since the last dream."""
        return _now() - self.since >= TIME_GATE

    @property
    def sessions_ok(self) -> bool:
        """At least :data:`SESSION_GATE` sessions since the last dream."""
        return self.sessions >= SESSION_GATE

    @property
    def lock_ok(self) -> bool:
        """No other dream holds the lock."""
        return self.lock_holder is None

    @property
    def open(self) -> bool:
        """All three gates pass: an automatic dream may run."""
        return self.time_ok and self.sessions_ok and self.lock_ok


def check_gates(agent_dir: Path, sessions_dir: Path) -> GateReport:
    """Evaluate the three gates (reads files; call off the UI thread)."""
    since = last_dream(agent_dir)
    return GateReport(
        since=since,
        sessions=sessions_since(sessions_dir, since),
        lock_holder=_lock_holder(agent_dir),
    )


def _lock_holder(agent_dir: Path) -> str | None:
    """Who holds a live lock, or None when it is free or stale."""
    try:
        info = json.loads((agent_dir / _LOCK).read_text(encoding="utf-8"))
        taken = datetime.fromisoformat(info["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if _now() - taken > STALE_LOCK:
        return None
    return f"pid {info.get('pid')} since {taken.astimezone().strftime('%H:%M')}"


class DreamLock:
    """The lock gate: an exclusive lock file; ``acquire()`` returns False if held."""

    def __init__(self, agent_dir: Path) -> None:  # noqa: D107
        self._path = agent_dir / _LOCK
        self._held = False

    def acquire(self) -> bool:
        """Take the lock, replacing a stale one; False when another dream holds it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _lock_holder(self._path.parent) is not None:
                    return False
                # Stale (or unreadable): remove and retry once. A racing process
                # that wins the retry keeps the lock; we then report it held.
                try:
                    self._path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "at": _now().isoformat()}, stream)
            self._held = True
            return True
        return False

    def release(self) -> None:
        """Give the lock back (only if this object took it)."""
        if self._held:
            self._held = False
            with contextlib.suppress(OSError):
                self._path.unlink()
