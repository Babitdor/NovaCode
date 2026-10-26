"""Transactional, root-scoped coordination storage."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from novacode_cli.teams.models import Identity, Limits, TeamError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

Record = dict[str, Any]


def _text(value: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        msg = "Non-empty text exceeds the permitted limit"
        raise TeamError(msg)
    return value


class TeamStore:
    """Parent-owned durable task board and inboxes."""

    def __init__(self, path: Path, limits: Limits | None = None) -> None:
        """Open an explicit profile path; callers create this lazily off-loop."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = limits or Limits()
        self._lock = threading.RLock()
        self._owner: tuple[Identity, str] | None = None
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS team_records (
              project TEXT NOT NULL, root TEXT NOT NULL, kind TEXT NOT NULL,
              id TEXT NOT NULL, team TEXT NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY(project,root,kind,id));
            CREATE INDEX IF NOT EXISTS team_records_scope
              ON team_records(project,root,kind,team);
            CREATE TABLE IF NOT EXISTS team_events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
              root TEXT NOT NULL, team TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS team_owner (
              project TEXT NOT NULL, root TEXT NOT NULL, token TEXT NOT NULL,
              pid INTEGER NOT NULL, born REAL NOT NULL, PRIMARY KEY(project,root));
        """)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self._owner is not None:
                    caller, token = self._owner
                    row = self.db.execute(
                        "SELECT token FROM team_owner WHERE project=? AND root=?",
                        (caller.project, caller.root),
                    ).fetchone()
                    if row is None or row[0] != token:
                        msg = "Coordinator ownership was lost"
                        raise TeamError(msg)  # noqa: TRY301 - Rollback must include validation failures.
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def acquire_owner(self, caller: Identity, token: str) -> None:
        """Reject a second live parent atomically; PID reuse is not ownership."""
        import psutil

        with self._transaction():
            row = self.db.execute(
                "SELECT token,pid,born FROM team_owner WHERE project=? AND root=?",
                (caller.project, caller.root),
            ).fetchone()
            if row and row[0] != token:
                try:
                    alive = psutil.Process(row[1]).create_time() == row[2]
                except psutil.NoSuchProcess:
                    alive = False
                except psutil.AccessDenied as exc:
                    msg = "Cannot establish coordinator ownership"
                    raise TeamError(msg) from exc
                if alive:
                    msg = "This root already has a live coordinator owner"
                    raise TeamError(msg)
            born = psutil.Process(os.getpid()).create_time()
            self.db.execute(
                "INSERT INTO team_owner VALUES(?,?,?,?,?) ON CONFLICT(project,root) "
                "DO UPDATE SET token=excluded.token,pid=excluded.pid,born=excluded.born",
                (caller.project, caller.root, token, os.getpid(), born),
            )
        self._owner = caller, token

    def release_owner(self) -> None:
        """Release only this instance's fencing token, never another parent's."""
        if self._owner is not None:
            caller, token = self._owner
            with self._transaction():
                self.db.execute(
                    "DELETE FROM team_owner WHERE project=? AND root=? AND token=?",
                    (caller.project, caller.root, token),
                )
            self._owner = None

    def _get(self, caller: Identity, kind: str, key: str) -> Record:
        row = self.db.execute(
            "SELECT body FROM team_records WHERE project=? AND root=? AND kind=? AND id=?",
            (caller.project, caller.root, kind, key),
        ).fetchone()
        if row is None:
            msg = "Unknown or unauthorized resource"
            raise TeamError(msg)
        return json.loads(row[0])

    def _rows(self, caller: Identity, kind: str, team: str) -> list[Record]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT body FROM team_records WHERE project=? AND root=? AND kind=? AND team=? "
                "ORDER BY rowid",
                (caller.project, caller.root, kind, team),
            )
        ]

    def _put(self, caller: Identity, kind: str, value: Record) -> Record:
        encoded = json.dumps(value, allow_nan=False)
        self.db.execute(
            "INSERT INTO team_records VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(project,root,kind,id) DO UPDATE SET body=excluded.body",
            (caller.project, caller.root, kind, value["id"], value["team"], encoded),
        )
        self.db.execute(
            "INSERT INTO team_events(project,root,team,body) VALUES(?,?,?,?)",
            (
                caller.project,
                caller.root,
                value["team"],
                json.dumps(
                    {
                        "kind": kind,
                        "id": value["id"],
                        "status": value.get("status"),
                        "version": value.get("version", 0),
                        "actor": caller.agent,
                    }
                ),
            ),
        )
        self.db.execute(
            "DELETE FROM team_events WHERE project=? AND root=? AND seq NOT IN "
            "(SELECT seq FROM team_events WHERE project=? AND root=? ORDER BY seq DESC LIMIT 4096)",
            (caller.project, caller.root, caller.project, caller.root),
        )
        return value

    def _access(self, caller: Identity, team: str, *, lead_only: bool = False) -> Record:
        value = self._get(caller, "team", team)
        if caller.agent != "lead":
            member = self._get(caller, "agent", caller.agent)
            if (
                lead_only
                or caller.team != team
                or member["team"] != team
                or member["status"] in {"stopped", "failed", "disconnected"}
            ):
                msg = "Operation is not authorized for this agent"
                raise TeamError(msg)
        return value

    def _active(self, caller: Identity, team: str, *, lead_only: bool = False) -> Record:
        value = self._access(caller, team, lead_only=lead_only)
        if value["status"] != "active":
            msg = "Team is not active"
            raise TeamError(msg)
        return value

    def create_team(self, caller: Identity, name: str, *, swarm: bool = False) -> Record:
        """Create a lead-owned team without starting a worker."""
        if caller.agent != "lead":
            msg = "Only the lead can create teams"
            raise TeamError(msg)
        _text(name, 200)
        key = uuid.uuid4().hex
        with self._transaction():
            count = self.db.execute(
                "SELECT COUNT(*) FROM team_records WHERE project=? AND root=? AND kind='team'",
                (caller.project, caller.root),
            ).fetchone()[0]
            if count >= self.limits.tasks:
                msg = "Team limit reached"
                raise TeamError(msg)
            return self._put(
                caller,
                "team",
                {
                    "id": key,
                    "team": key,
                    "name": name,
                    "status": "active",
                    "swarm": swarm,
                },
            )

    def get_team(self, caller: Identity, team: str) -> Record:
        """Return only a team visible to the bound caller."""
        with self._lock:
            return self._access(caller, team)

    def teams(self, caller: Identity) -> list[Record]:
        """List only this root's teams (members see their own team only)."""
        with self._lock:
            if caller.agent != "lead":
                return [self._access(caller, caller.team or "")]
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM team_records WHERE project=? AND root=? AND kind='team'",
                    (caller.project, caller.root),
                )
            ]

    def set_team_status(self, caller: Identity, team: str, status: str) -> Record:
        """Drain before archiving; never delete persisted evidence."""
        with self._transaction():
            value = self._access(caller, team, lead_only=True)
            if (value["status"], status) not in {("active", "draining"), ("draining", "archived")}:
                msg = "Invalid team transition"
                raise TeamError(msg)
            value["status"] = status
            return self._put(caller, "team", value)

    def add_agent(
        self,
        caller: Identity,
        team: str,
        name: str,
        mode: str,
        cwd: str,
        *,
        parent: str | None = None,
    ) -> Record:
        """Reserve a bounded persistent identity before starting execution."""
        _text(name, 200)
        if mode not in {"default", "fork", "worktree", "remote"}:
            msg = "Unknown spawn mode"
            raise TeamError(msg)
        with self._transaction():
            self._active(caller, team, lead_only=True)
            members = self._rows(caller, "agent", team)
            if len(members) >= self.limits.agents:
                msg = "Agent limit reached"
                raise TeamError(msg)
            return self._put(
                caller,
                "agent",
                {
                    "id": uuid.uuid4().hex,
                    "team": team,
                    "name": name,
                    "mode": mode,
                    "cwd": cwd,
                    "parent": parent or caller.agent,
                    "status": "starting",
                    "version": 0,
                },
            )

    def agents(self, caller: Identity, team: str) -> list[Record]:
        """List bounded public lifecycle records, never conversations."""
        with self._lock:
            self._access(caller, team)
            return self._rows(caller, "agent", team)

    def agent(self, caller: Identity, key: str) -> Record:
        """Resolve stable IDs before labels and enforce membership."""
        with self._lock:
            value = self._get(caller, "agent", key)
            self._access(caller, value["team"])
            return value

    def agent_status(self, caller: Identity, key: str, status: str, **metadata: Any) -> Record:
        """Host-only lifecycle update; this method is not a model-facing tool."""
        if status not in {
            "starting",
            "idle",
            "running",
            "waiting_approval",
            "stopping",
            "stopped",
            "failed",
            "disconnected",
        }:
            msg = "Unknown agent status"
            raise TeamError(msg)
        with self._transaction():
            value = self._get(caller, "agent", key)
            self._access(caller, value["team"], lead_only=True)
            value.update(metadata, status=status, version=value["version"] + 1)
            return self._put(caller, "agent", value)

    def create_task(
        self,
        caller: Identity,
        team: str,
        title: str,
        description: str,
        depends_on: list[str] | None = None,
    ) -> Record:
        """Create a bounded task with existing same-team prerequisites."""
        _text(title, 500)
        _text(description, self.limits.message_bytes)
        with self._transaction():
            self._active(caller, team, lead_only=True)
            if len(self._rows(caller, "task", team)) >= self.limits.tasks:
                msg = "Task limit reached"
                raise TeamError(msg)
            dependencies = list(dict.fromkeys(depends_on or []))
            for dependency in dependencies:
                if self._get(caller, "task", dependency)["team"] != team:
                    msg = "Cross-team dependency"
                    raise TeamError(msg)
            return self._put(
                caller,
                "task",
                {
                    "id": uuid.uuid4().hex,
                    "team": team,
                    "title": title,
                    "description": description,
                    "depends_on": dependencies,
                    "status": "pending",
                    "owner": None,
                    "version": 0,
                    "generation": 0,
                    "lease_until": None,
                    "result": None,
                },
            )

    def get_task(self, caller: Identity, key: str) -> Record:
        """Read one task within the caller's team boundary."""
        with self._lock:
            value = self._get(caller, "task", key)
            self._access(caller, value["team"])
            return value

    def tasks(self, caller: Identity, team: str) -> list[Record]:
        """Read the bounded shared board."""
        with self._lock:
            self._access(caller, team)
            return self._rows(caller, "task", team)

    def claim(
        self, caller: Identity, key: str, expected_version: int, *, now: float | None = None
    ) -> Record:
        """Atomically claim one pending task; expired work is never replayed."""
        with self._transaction():
            value = self._get(caller, "task", key)
            self._active(caller, value["team"])
            if value["version"] != expected_version or value["status"] != "pending":
                msg = "Task version conflict or task already claimed"
                raise TeamError(msg)
            for dependency in value["depends_on"]:
                if self._get(caller, "task", dependency)["status"] != "completed":
                    msg = "Task dependencies are incomplete"
                    raise TeamError(msg)
            value.update(
                status="running",
                owner=caller.agent,
                version=expected_version + 1,
                generation=value["generation"] + 1,
                lease_until=(time.time() if now is None else now) + self.limits.lease_seconds,
            )
            return self._put(caller, "task", value)

    def update_task(
        self,
        caller: Identity,
        key: str,
        expected_version: int,
        status: str,
        result: Record | None = None,
        *,
        now: float | None = None,
    ) -> Record:
        """Accept only a current owner's fenced completion or lead cancellation."""
        with self._transaction():
            value = self._get(caller, "task", key)
            self._access(caller, value["team"])
            if value["version"] != expected_version:
                msg = "Task version conflict"
                raise TeamError(msg)
            if caller.agent not in {"lead", value["owner"]}:
                msg = "Task belongs to another agent"
                raise TeamError(msg)
            if status not in {"completed", "failed", "blocked", "cancelled"}:
                msg = "Invalid task transition"
                raise TeamError(msg)
            if value["status"] not in {"pending", "running", "blocked"}:
                msg = "Task is already terminal"
                raise TeamError(msg)
            if status == "completed" and (value["status"] != "running" or not result):
                msg = "Completion requires running ownership and evidence"
                raise TeamError(msg)
            timestamp = time.time() if now is None else now
            if (
                value["lease_until"] is not None
                and timestamp > value["lease_until"]
                and status in {"completed", "failed"}
            ):
                msg = "Task lease expired; reconcile the worker before retrying"
                raise TeamError(msg)
            if (
                result is not None
                and len(json.dumps(result, allow_nan=False).encode()) > self.limits.result_bytes
            ):
                msg = "Result limit reached; use artifact references"
                raise TeamError(msg)
            value.update(
                status=status, result=result, version=expected_version + 1, lease_until=None
            )
            return self._put(caller, "task", value)

    def renew(self, caller: Identity, key: str, generation: int) -> None:
        """Host heartbeat renews only a live, matching generation."""
        with self._transaction():
            value = self._get(caller, "task", key)
            self._active(caller, value["team"])
            if (
                value["owner"] != caller.agent
                or value["generation"] != generation
                or value["status"] != "running"
                or value["lease_until"] < time.time()
            ):
                msg = "Cannot renew stale ownership"
                raise TeamError(msg)
            value["lease_until"] = time.time() + self.limits.lease_seconds
            self._put(caller, "task", value)

    def resolve(self, caller: Identity, team: str, target: str) -> str:
        """Resolve stable IDs or unambiguous display names within one team."""
        self._access(caller, team)
        if target == "lead":
            return target
        members = self._rows(caller, "agent", team)
        exact = [member for member in members if member["id"] == target]
        matches = exact or [member for member in members if member["name"] == target]
        if len(matches) != 1:
            msg = "Unknown or ambiguous agent; use its stable ID"
            raise TeamError(msg)
        if matches[0]["status"] in {"failed", "stopped", "disconnected"}:
            msg = "Recipient is unavailable"
            raise TeamError(msg)
        return matches[0]["id"]

    def send(
        self,
        caller: Identity,
        team: str,
        target: str,
        text: str,
        request_id: str,
        reply_to: str | None = None,
    ) -> Record:
        """Persist before delivery, with scoped idempotency and backpressure."""
        _text(text, self.limits.message_bytes)
        _text(request_id, 200)
        with self._transaction():
            self._active(caller, team)
            recipient = self.resolve(caller, team, target)
            rows = self._rows(caller, "message", team)
            for message in rows:
                if message["sender"] == caller.agent and message["request_id"] == request_id:
                    if (message["recipient"], message["text"], message["reply_to"]) != (
                        recipient,
                        text,
                        reply_to,
                    ):
                        msg = "Idempotency request reused with different content"
                        raise TeamError(msg)
                    return message
            if reply_to:
                previous = self._get(caller, "message", reply_to)
                if previous["team"] != team or previous["recipient"] != caller.agent:
                    msg = "Cannot reply to another agent's inbox"
                    raise TeamError(msg)
                if previous["depth"] >= self.limits.message_depth:
                    msg = "Message chain limit reached"
                    raise TeamError(msg)
                depth = previous["depth"] + 1
            else:
                depth = 0
            pending = [m for m in rows if not m["acknowledged"]]
            if (
                len(rows) >= self.limits.messages
                or sum(m["recipient"] == recipient for m in pending) >= self.limits.pending_messages
            ):
                msg = "Inbox limit reached; acknowledge or archive messages first"
                raise TeamError(msg)
            return self._put(
                caller,
                "message",
                {
                    "id": uuid.uuid4().hex,
                    "team": team,
                    "sender": caller.agent,
                    "recipient": recipient,
                    "text": text,
                    "request_id": request_id,
                    "reply_to": reply_to,
                    "depth": depth,
                    "acknowledged": False,
                    "created": time.time(),
                },
            )

    def inbox(self, caller: Identity, team: str) -> list[Record]:
        """Read only the bound caller's unacknowledged messages, oldest first."""
        with self._lock:
            self._access(caller, team)
            return [
                message
                for message in self._rows(caller, "message", team)
                if message["recipient"] == caller.agent and not message["acknowledged"]
            ]

    def ack(self, caller: Identity, key: str) -> None:
        """Acknowledge delivery without claiming completion of the requested work."""
        with self._transaction():
            value = self._get(caller, "message", key)
            self._access(caller, value["team"])
            if value["recipient"] != caller.agent:
                msg = "Cannot acknowledge another agent's message"
                raise TeamError(msg)
            value["acknowledged"] = True
            self._put(caller, "message", value)

    def record_usage(self, caller: Identity, key: str, turn_id: str, usage: Record) -> Record:
        """Persist the existing usage tree once per worker turn, including unknown usage."""
        from novacode_cli.tracking.usage_tree import UsageTree

        _text(turn_id, 200)
        with self._transaction():
            agent = self._get(caller, "agent", key)
            self._access(caller, agent["team"], lead_only=True)
            record_id = f"{key}:{turn_id}"
            previous = self.db.execute(
                "SELECT body FROM team_records WHERE project=? AND root=? "
                "AND kind='usage' AND id=?",
                (caller.project, caller.root, record_id),
            ).fetchone()
            if previous:
                if json.loads(previous[0])["usage"] != usage:
                    msg = "Usage turn replay changed its payload"
                    raise TeamError(msg)
                return agent
            tree = UsageTree.from_dict(usage) if "root" in usage else None
            totals = tree.root.totals() if tree is not None else {}
            accumulated = agent.setdefault("usage_totals", {})
            for name, value in totals.items():
                accumulated[name] = accumulated.get(name, 0) + value
            cost = tree.root.cost(recursive=True) if tree is not None else None
            if cost is None or agent.get("usage_cost_unknown"):
                agent["usage_cost_unknown"] = True
                agent["usage_cost_usd"] = None
            else:
                agent["usage_cost_usd"] = agent.get("usage_cost_usd", 0.0) + cost
            self._put(caller, "usage", {"id": record_id, "team": agent["team"], "usage": usage})
            return self._put(caller, "agent", agent)

    def close(self) -> None:
        """Release the parent connection after workers stop."""
        with self._lock:
            self.release_owner()
            self.db.close()

    def teammate_state(self, caller: Identity) -> Record | None:
        """Read profile controller state; never expose it through team tools."""
        with self._lock:
            row = self.db.execute(
                "SELECT body FROM team_records WHERE project=? AND root=? "
                "AND kind='teammate' AND id='profile'",
                (caller.project, caller.root),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_teammate_state(self, caller: Identity, value: Record) -> None:
        """Fence durable controller updates with the existing process owner."""
        if caller.agent != "lead":
            message = "Only the controller owns teammate state"
            raise TeamError(message)
        with self._transaction():
            self._put(caller, "teammate", {**value, "id": "profile", "team": "profile"})
