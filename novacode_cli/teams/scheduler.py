"""Bounded process reservations shared with ordinary session tabs."""

from __future__ import annotations

import threading

from novacode_cli.teams.models import TeamError


class ProcessBudget:
    """Reserve before awaiting launch; release once on verified termination."""

    def __init__(self, limit: int = 4) -> None:
        """Create a host-local budget with no event-loop affinity."""
        self.limit = limit
        self._keys: set[str] = set()
        self._lock = threading.Lock()

    def reserve(self, key: str) -> None:
        """Reserve a unique slot atomically, including concurrent startups."""
        with self._lock:
            if key in self._keys or len(self._keys) >= self.limit:
                msg = "Process limit reached or duplicate reservation"
                raise TeamError(msg)
            self._keys.add(key)

    def adopt(self, key: str) -> None:
        """Count discovered orphan workers even when they already exceed the limit."""
        with self._lock:
            self._keys.add(key)

    def release(self, key: str) -> None:
        """Idempotently release a slot whose process has stopped."""
        with self._lock:
            self._keys.discard(key)

    @property
    def used(self) -> int:
        """Number of live or starting workers, including session tabs."""
        with self._lock:
            return len(self._keys)


PROCESS_BUDGET = ProcessBudget()
