"""Atomic factory snapshots with optimistic writer fencing."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock
from typing import TYPE_CHECKING, Any

from novacode_cli.prime_factory import DEFAULT_OUTPUT_LIMIT, VERSION

if TYPE_CHECKING:
    from pathlib import Path


class FactoryError(ValueError):
    """A factory invariant requires intervention before work can continue."""


def listing(record: dict[str, Any]) -> dict[str, Any]:
    """Project bounded display metadata separately from execution and publication artifacts."""
    result = {
        key: record[key]
        for key in (
            "id",
            "version",
            "factory",
            "thread",
            "root",
            "goal",
            "stage",
            "phase",
            "status",
        )
        if key in record
    }
    result["summary_only"] = True
    result["checks"] = [
        {"id": check["id"], "status": check["status"], "purpose": check.get("purpose", "")[:300]}
        for check in record.get("checks", [])
    ]
    return result


class FactoryStore:
    """Commit complete snapshots before dispatching side effects."""

    def __init__(self, path: Path) -> None:
        """Open repository-local storage; failures propagate to the caller."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, revision INTEGER, body TEXT)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "summary" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN summary TEXT")
        self.db.commit()

    def save(self, record: dict[str, Any]) -> None:
        """Reject stale controllers rather than allowing two schedulers to dispatch."""
        with self.lock:
            revision = record.get("store_revision", 0)
            snapshot = {**record, "store_revision": revision + 1}
            encoded = json.dumps(snapshot, allow_nan=False, sort_keys=True)
            from novacode_cli.security.prompt_injection import _credential_values

            if any(json.dumps(secret)[1:-1] in encoded for secret in _credential_values()):
                message = "Factory records cannot contain credentials; use environment references"
                raise FactoryError(message)
            summary = json.dumps(listing(snapshot), allow_nan=False)
            with self.db:
                if revision == 0:
                    self.db.execute(
                        "INSERT INTO runs (id, revision, body, summary) VALUES (?, ?, ?, ?)",
                        (record["id"], 1, encoded, summary),
                    )
                else:
                    cursor = self.db.execute(
                        "UPDATE runs SET revision=?, body=?, summary=? WHERE id=? AND revision=?",
                        (revision + 1, encoded, summary, record["id"], revision),
                    )
                    if cursor.rowcount != 1:
                        msg = "Factory ownership changed; reload before continuing"
                        raise FactoryError(msg)
            record["store_revision"] = revision + 1

    def load(self, identity: str) -> dict[str, Any]:
        """Read only this repository's versioned factory records."""
        with self.lock:
            row = self.db.execute("SELECT body FROM runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            msg = "Factory run not found in this repository"
            raise FactoryError(msg)
        record = json.loads(row[0])
        if record.get("version") != VERSION or record.get("factory") is not True:
            msg = "Unsupported factory record version"
            raise FactoryError(msg)
        record.setdefault("output_token_limit", DEFAULT_OUTPUT_LIMIT)
        return record

    def records(self) -> list[dict[str, Any]]:
        """Return bounded recent summaries without loading command output."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, summary FROM runs ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
            result = []
            for identity, stored_summary in rows:
                summary = stored_summary
                if summary is None:
                    # Upgrade one old record at a time, never materialize fifty full histories.
                    record = self.load(identity)
                    summary = json.dumps(listing(record))
                    with self.db:
                        self.db.execute(
                            "UPDATE runs SET summary=? WHERE id=? AND revision=?",
                            (summary, identity, record["store_revision"]),
                        )
                result.append(json.loads(summary))
            return result

    def close(self) -> None:
        """Release the database after all owned workers have stopped."""
        self.db.close()
