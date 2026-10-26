"""Disposable, owner-private SQLite knowledge artifacts and producer fences."""
# ruff: noqa: ANN401, EM101, TRY003

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

TTL = 7 * 24 * 3600
MAX_ARTIFACTS = 2000
MAX_PAYLOAD = 64 * 1024 * 1024
LEASE_SECONDS = 90


def digest(value: str | bytes) -> str:
    """Hash identities or complete source bytes without storing the original."""
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _protect(directory: Path) -> None:
    """Restrict the directory before creating SQLite and its WAL sidecars."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError("Knowledge directory must not contain links")
    if os.name != "nt":
        directory.chmod(0o700)
        return
    executable = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/icacls.exe"
    identity = subprocess.run(  # noqa: S603
        [str(executable.with_name("whoami.exe")), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sid = next(csv.reader([identity.stdout.strip()]))[1]
    if not sid.startswith("S-1-") or any(c not in "S0123456789-" for c in sid):
        raise OSError("Could not determine knowledge directory owner")
    subprocess.run(  # noqa: S603
        [str(executable), str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class KnowledgeCache:
    """Short independent transactions; never hold a connection across model work."""

    def __init__(self, directory: Path) -> None:
        """Defer filesystem and database work until a lookup or command."""
        self.directory = directory.absolute()
        self._ready = False
        self._lock = RLock()

    @contextmanager
    def connection(self) -> Any:
        """Serialize lazy initialization and short writes within this instance."""
        with self._lock, self._connection() as db:
            yield db

    @contextmanager
    def _connection(self) -> Any:
        """Open a bounded writer transaction on a private disposable database."""
        if not self._ready:
            _protect(self.directory)
        path = self.directory / "knowledge.sqlite3"
        if path.is_symlink():
            raise ValueError("Knowledge database must not be a link")
        db = sqlite3.connect(path, timeout=0.5)
        db.row_factory = sqlite3.Row
        try:
            if not self._ready:
                self._wal(db)
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS namespaces (
                        ns TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
                        semantic_cache_enabled INTEGER NOT NULL DEFAULT 1,
                        stats TEXT NOT NULL DEFAULT '{}');
                    CREATE TABLE IF NOT EXISTS artifacts (
                        ns TEXT NOT NULL, key TEXT NOT NULL, refs TEXT NOT NULL,
                        summary TEXT NOT NULL, model TEXT NOT NULL,
                        embedding TEXT NOT NULL, vector TEXT, created REAL NOT NULL,
                        expires REAL NOT NULL, accessed REAL NOT NULL, bytes INTEGER NOT NULL,
                        PRIMARY KEY(ns,key));
                    CREATE TABLE IF NOT EXISTS leases (
                        ns TEXT NOT NULL, key TEXT NOT NULL, epoch INTEGER NOT NULL,
                        owner TEXT NOT NULL, until REAL NOT NULL, PRIMARY KEY(ns,key));
                    CREATE INDEX IF NOT EXISTS artifact_access ON artifacts(accessed);
                """)
                self._ready = True
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _wal(db: sqlite3.Connection) -> None:
        """Bound retries for SQLite's non-waiting initial journal-mode upgrade."""
        deadline = time.monotonic() + 0.5
        while True:
            try:
                db.execute("PRAGMA journal_mode=WAL")
                return  # noqa: TRY300
            except sqlite3.OperationalError as exc:
                if (
                    getattr(exc, "sqlite_errorcode", 0)
                    not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.025)

    @staticmethod
    def namespace(root: Path, assistant: str) -> str:
        """Canonical workspace plus assistant identity is the sharing boundary."""
        return digest(json.dumps([os.path.normcase(str(root.resolve())), assistant]))

    @staticmethod
    def _state(db: sqlite3.Connection, ns: str) -> sqlite3.Row:
        db.execute("INSERT OR IGNORE INTO namespaces(ns) VALUES (?)", (ns,))
        return db.execute("SELECT * FROM namespaces WHERE ns=?", (ns,)).fetchone()

    def state(self, ns: str) -> dict:
        """Read the immediately authoritative enable flag and epoch."""
        with self.connection() as db:
            return dict(self._state(db, ns))

    def control(self, ns: str, action: str) -> dict:
        """Clear/off fence old producers; each namespace retains its own toggle."""
        with self.connection() as db:
            self._state(db, ns)
            if action in {"clear", "off"}:
                db.execute("UPDATE namespaces SET epoch=epoch+1 WHERE ns=?", (ns,))
                db.execute("DELETE FROM artifacts WHERE ns=?", (ns,))
                db.execute("DELETE FROM leases WHERE ns=?", (ns,))
            if action in {"on", "off"}:
                db.execute(
                    "UPDATE namespaces SET semantic_cache_enabled=? WHERE ns=?",
                    (int(action == "on"), ns),
                )
            self._evict(db, ns)
            state = self._state(db, ns)
            stats = json.loads(state["stats"])
            retained = db.execute(
                "SELECT coalesce(sum(bytes),0) FROM artifacts WHERE ns=?", (ns,)
            ).fetchone()[0]
            return {
                "semantic_cache_enabled": bool(state["semantic_cache_enabled"]),
                "epoch": state["epoch"],
                "retained_bytes": retained,
                **stats,
            }

    def event(self, ns: str, name: str, value: float = 1) -> None:
        """Persist only numeric accounting, never queries or source content."""
        with self.connection() as db:
            stats = json.loads(self._state(db, ns)["stats"])
            stats[name] = stats.get(name, 0) + value
            db.execute("UPDATE namespaces SET stats=? WHERE ns=?", (json.dumps(stats), ns))

    def candidates(self, ns: str, model: str, key: str | None) -> list[dict]:
        """Retrieve bounded metadata; callers must validate every returned source."""
        with self.connection() as db:
            self._evict(db, ns)
            rows = db.execute(
                "SELECT * FROM artifacts WHERE ns=? AND model=?" + (" AND key=?" if key else ""),  # noqa: S608 - fixed SQL suffix.
                (ns, model, key) if key else (ns, model),
            ).fetchall()
            return [dict(row) for row in rows]

    def touch(self, ns: str, key: str, epoch: int) -> bool:
        """Validate the fence and update access after source validation."""
        with self.connection() as db:
            state = self._state(db, ns)
            if state["epoch"] != epoch or not state["semantic_cache_enabled"]:
                return False
            return bool(
                db.execute(
                    "UPDATE artifacts SET accessed=? WHERE ns=? AND key=?",
                    (self._access(db), ns, key),
                ).rowcount
            )

    def invalidate(self, ns: str, key: str) -> None:
        """Discard a source-invalid artifact."""
        with self.connection() as db:
            db.execute("DELETE FROM artifacts WHERE ns=? AND key=?", (ns, key))
        self.event(ns, "invalidations")

    def acquire(self, ns: str, key: str, epoch: int, owner: str) -> bool:
        """Lease a single producer across processes, expiring crashed producers."""
        with self.connection() as db:
            state = self._state(db, ns)
            if state["epoch"] != epoch or not state["semantic_cache_enabled"]:
                return False
            db.execute("DELETE FROM leases WHERE until<=?", (time.time(),))
            return bool(
                db.execute(
                    "INSERT OR IGNORE INTO leases VALUES (?,?,?,?,?)",
                    (ns, key, epoch, owner, time.time() + LEASE_SECONDS),
                ).rowcount
            )

    def release(self, ns: str, key: str, owner: str) -> None:
        """Release only this producer's lease, never a replacement lease."""
        with self.connection() as db:
            db.execute("DELETE FROM leases WHERE ns=? AND key=? AND owner=?", (ns, key, owner))

    def publish(
        self,
        ns: str,
        key: str,
        epoch: int,
        owner: str,
        refs: str,
        summary: str,
        model: str,
        embedding: str,
        vector: list[float] | None,
    ) -> bool:
        """Commit only while both the namespace fence and producer lease match."""
        payload = json.dumps(vector, allow_nan=False) if vector is not None else None
        size = len((refs + summary + model + embedding + (payload or "")).encode())
        with self.connection() as db:
            state = self._state(db, ns)
            lease = db.execute("SELECT * FROM leases WHERE ns=? AND key=?", (ns, key)).fetchone()
            if (
                state["epoch"] != epoch
                or not state["semantic_cache_enabled"]
                or not lease
                or lease["owner"] != owner
                or lease["until"] <= time.time()
            ):
                return False
            now = time.time()
            db.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ns,
                    key,
                    refs,
                    summary,
                    model,
                    embedding,
                    payload,
                    now,
                    now + TTL,
                    self._access(db),
                    size,
                ),
            )
            self._evict(db, ns)
            return True

    @staticmethod
    def _access(db: sqlite3.Connection) -> int:
        """Order touches even when the platform clock repeats or moves backwards."""
        previous = db.execute("SELECT coalesce(max(accessed),0) FROM artifacts").fetchone()[0]
        return max(time.time_ns() // 1000, int(previous) + 1)

    @staticmethod
    def _evict(db: sqlite3.Connection, ns: str) -> None:
        db.execute("DELETE FROM artifacts WHERE expires<=?", (time.time(),))
        db.execute(
            "DELETE FROM artifacts WHERE ns=? AND key IN "
            "(SELECT key FROM artifacts WHERE ns=? ORDER BY accessed DESC LIMIT -1 OFFSET ?)",
            (ns, ns, MAX_ARTIFACTS),
        )
        total = db.execute("SELECT coalesce(sum(bytes),0) FROM artifacts").fetchone()[0]
        while total > MAX_PAYLOAD:
            row = db.execute(
                "SELECT ns,key,bytes FROM artifacts ORDER BY accessed LIMIT 1"
            ).fetchone()
            db.execute("DELETE FROM artifacts WHERE ns=? AND key=?", (row["ns"], row["key"]))
            total -= row["bytes"]
