"""Versioned generated knowledge. SQLite is authoritative; exports are disposable."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterator


def auto_approve_enabled() -> bool:
    """The user's /learning auto setting; off when it cannot be read."""
    try:
        from novacode_cli.config.nova_config import NovaConfig

        return NovaConfig().get_learning_auto_approve()
    except Exception:  # noqa: BLE001 - an unreadable setting must not approve anything
        return False


@dataclass(frozen=True)
class LearningRecord:
    """One immutable revision of an evidence-bearing knowledge proposal."""

    id: str
    revision: int
    origin: str
    scope: str
    project: str
    session: str
    task: str
    kind: str
    content: str
    applicability: str = ""
    evidence: tuple[str, ...] = ()
    status: str = "candidate"
    supersedes: str | None = None
    version: int = 1
    target: str = ""
    source_hash: str = ""
    required_variables: tuple[str, ...] = ()
    approval_reference: str | None = None

    @property
    def reference(self) -> str:
        """Return the version-specific decision identifier."""
        return f"{self.id}@{self.revision}"


@dataclass(frozen=True)
class LearningProposal(LearningRecord):
    """A versioned intended change; approval never implies application."""

    version: int = 2

    @property
    def intended_changes(self) -> str:
        """Return the exact content authorized by revision-specific approval."""
        return self.content


class LearningLedger:
    """Each operation opens a connection; transactions serialize cross-process writes."""

    def __init__(self, path: Path) -> None:
        """Open or initialize the dedicated ledger without touching instructions."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS revisions (id TEXT, revision INTEGER, "
                "payload TEXT NOT NULL, PRIMARY KEY(id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (reference TEXT, action TEXT, timestamp REAL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS imports (identity TEXT PRIMARY KEY)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(payload: str) -> LearningRecord:
        data = json.loads(payload)
        data["evidence"] = tuple(data.get("evidence", ()))
        data["required_variables"] = tuple(data.get("required_variables", ()))
        proposal_version = 2
        return (LearningProposal if data.get("version") == proposal_version else LearningRecord)(
            **data
        )

    @staticmethod
    def _insert(db: sqlite3.Connection, record: LearningRecord) -> None:
        db.execute(
            "INSERT INTO revisions VALUES (?, ?, ?)",
            (record.id, record.revision, json.dumps(asdict(record))),
        )

    def propose(
        self,
        *,
        content: str,
        project: str = "",
        session: str = "",
        task: str = "",
        scope: str = "project",
        kind: str = "lesson",
        evidence: tuple[str, ...] = (),
        origin: str = "review",
        applicability: str = "",
        target: str = "",
        source_hash: str = "",
        required_variables: tuple[str, ...] = (),
        auto_approve: bool = False,
    ) -> LearningRecord:
        """Store an unverified candidate without activating its content.

        With ``auto_approve`` it is approved at once, exactly as ``/learning
        approve`` would (an event is recorded, ``rollback`` undoes it), and is
        applied at the next turn boundary.
        """
        if scope not in {"project", "shared"}:
            msg = "Unknown learning scope"
            raise ValueError(msg)
        record = LearningProposal(
            uuid.uuid4().hex,
            1,
            origin,
            scope,
            project,
            session,
            task,
            kind,
            content,
            applicability,
            evidence,
            target=target,
            source_hash=source_hash,
            required_variables=required_variables,
        )
        with self._connect() as db:
            self._insert(db, record)
        if auto_approve:
            return self.transition(record.reference, "approve")
        return record

    def list(self, *, project: str | None = None, active: bool = False) -> list[LearningRecord]:
        """Retrieve current revisions, project lessons before approved shared data."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM revisions r WHERE revision="
                "(SELECT MAX(revision) FROM revisions WHERE id=r.id) ORDER BY rowid"
            ).fetchall()
        records = [self._decode(row[0]) for row in rows]
        if project is not None:
            records = [
                r
                for r in records
                if (r.scope == "project" and r.project == project)
                or (r.scope == "shared" and (not active or r.status == "applied"))
            ]
        if active:
            records = [r for r in records if r.status == "applied"]
        return sorted(records, key=lambda r: r.scope != "project")

    def history(self, identity: str) -> builtins.list[LearningRecord]:
        """Read every recoverable revision in order."""
        with self._connect() as db:
            return [
                self._decode(row[0])
                for row in db.execute(
                    "SELECT payload FROM revisions WHERE id=? ORDER BY revision", (identity,)
                )
            ]

    def transition(
        self, reference: str, action: str, *, _db: sqlite3.Connection | None = None
    ) -> LearningRecord:
        """Approval names the displayed version; any stale decision fails closed."""
        identity, sep, revision = reference.partition("@")
        if not sep or not revision.isdigit():
            msg = "Use the exact id@revision shown by /learning review"
            raise ValueError(msg)
        if action not in {"approve", "reject", "rollback", "apply", "fail", "finish_rollback"}:
            msg = "Unknown learning decision"
            raise ValueError(msg)
        with nullcontext(_db) if _db is not None else self._connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1",
                (identity,),
            ).fetchone()
            if row is None:
                msg = "Unknown proposal"
                raise ValueError(msg)
            prior = self._decode(row[0])
            if prior.revision != int(revision):
                msg = "Proposal changed; review its current revision first"
                raise ValueError(msg)
            if action == "approve" and prior.status != "candidate":
                msg = "Only a candidate may be approved"
                raise ValueError(msg)
            if action == "reject" and prior.status != "candidate":
                msg = "Only a candidate may be rejected; use rollback for approved changes"
                raise ValueError(msg)
            if action == "rollback" and prior.status not in {"applied", "approved"}:
                msg = "Only active knowledge may be rolled back"
                raise ValueError(msg)
            expected = {
                "apply": "approved",
                "fail": "approved",
                "finish_rollback": "rollback_pending",
            }
            if action in expected and prior.status != expected[action]:
                message = "Invalid application transition"
                raise ValueError(message)
            record = replace(
                prior,
                revision=prior.revision + 1,
                status={
                    "approve": "approved",
                    "reject": "rejected",
                    "rollback": "rollback_pending",
                    "apply": "applied",
                    "fail": "failed",
                    "finish_rollback": "rolled_back",
                }[action],
                supersedes=prior.reference,
                approval_reference=prior.reference
                if action == "approve"
                else prior.approval_reference,
            )
            self._insert(db, record)
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?)", (record.reference, action, time.time())
            )
        return record

    def revise(self, reference: str, content: str) -> LearningProposal:
        """Change a proposal and require fresh approval for the new content."""
        identity, _, version = reference.partition("@")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1",
                (identity,),
            ).fetchone()
            if row is None:
                msg = "Unknown proposal"
                raise ValueError(msg)
            prior = self._decode(row[0])
            if str(prior.revision) != version:
                msg = "Stale revision"
                raise ValueError(msg)
            record = replace(
                prior,
                revision=prior.revision + 1,
                content=content,
                status="candidate",
                supersedes=prior.reference,
                approval_reference=None,
            )
            record = LearningProposal(**{**asdict(record), "version": 2})
            self._insert(db, record)
        return record

    def import_legacy(self, paths: builtins.list[Path], *, project: str = "") -> int:
        """Copy generated legacy lessons as unverified candidates; retain originals."""
        count = 0
        for path in paths:
            content = path.read_text(encoding="utf-8")
            identity = hashlib.sha256((str(path.resolve()) + content).encode()).hexdigest()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM imports WHERE identity=?", (identity,)).fetchone():
                    continue
                record = LearningRecord(
                    uuid.uuid4().hex,
                    1,
                    "legacy",
                    "project",
                    project,
                    "",
                    "",
                    "lesson",
                    content,
                    evidence=(str(path),),
                )
                self._insert(db, record)
                db.execute("INSERT INTO imports VALUES (?)", (identity,))
                count += 1
        return count

    def export(self, destination: Path, *, project: str) -> None:
        """Publish a human-readable export atomically with serialized ordering."""
        # Hold the database writer lock through publication so concurrent exports
        # cannot overwrite a newer decision with an older snapshot.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = self.list(project=project, active=True)
            text = "# Approved generated knowledge\n\n" + "\n\n".join(
                f"## {r.reference} ({r.scope})\n\n{r.content}" for r in records
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".learning-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                Path(name).replace(destination)
            finally:
                Path(name).unlink(missing_ok=True)
