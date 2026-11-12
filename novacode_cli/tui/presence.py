"""Short-lived status snapshots shared by Nova terminals using one session directory."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)
TTL = 10.0


class TerminalPresence:
    """One owner file, atomic replacement, and expiry after an unclean shutdown."""

    def __init__(self, sessions_dir: Path) -> None:
        """Bind an owner to the active session store without doing any I/O."""
        self.directory = sessions_dir / ".live"
        self.path = self.directory / f"{uuid4().hex}.json"
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, rows: list[dict]) -> None:
        """Run off the UI thread; stale files never imply a live terminal."""
        with self._lock:
            if self._closed:
                return
            temporary = self.path.with_suffix(".tmp")
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary.write_text(
                    json.dumps({"expires": time.time() + TTL, "rows": rows}), encoding="utf-8"
                )
                temporary.replace(self.path)
            except OSError:
                logger.debug("Could not publish terminal presence", exc_info=True)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def close(self) -> None:
        """Serialize cleanup with any in-flight write, so it cannot resurrect us."""
        with self._lock:
            self._closed = True
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)


def read_presence(sessions_dir: Path) -> list[dict]:
    """Read fresh snapshots; malformed or concurrently replaced files are skipped."""
    rows: list[dict] = []
    now = time.time()
    for path in (sessions_dir / ".live").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expires = data["expires"]
            if not isinstance(expires, (int, float)) or not now < expires <= now + TTL * 2:
                continue
            rows.extend(row for row in data["rows"] if isinstance(row, dict))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return rows
