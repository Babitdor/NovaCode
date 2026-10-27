"""Transactional project conversations and bounded task execution."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.sessions.worktree import _git

if TYPE_CHECKING:
    from collections.abc import Iterator

AGENTS = ("nova", "claude", "codex")
MAX_TEXT = 100_000
MAX_WORKERS = 2
TASK_TURNS = 6
Record = dict[str, Any]


def canonical_project(path: Path) -> Path:
    """Resolve linked worktrees through the common Git directory."""
    code, listing, _ = _git(["worktree", "list", "--porcelain"], path)
    if code == 0:
        for line in listing.splitlines():
            if line.startswith("worktree "):
                return Path(line[len("worktree ") :]).resolve()
    return path.resolve()


def recipients(text: str, room: str) -> list[str]:
    """Select reserved standalone mentions, leaving file mentions untouched."""
    if room.startswith("dm:"):
        return [room[3:]]
    return list(
        dict.fromkeys(re.findall(r"(?<!\S)@(nova|claude|codex)(?=\s|$|[,!?;:])", text))
    ) or ["nova"]


def checked_text(value: str) -> str:
    """Bound persisted and model-supplied text."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        msg = "Text must contain 1-100000 characters"
        raise ValueError(msg)
    return value


class RoomStore:
    """One database per canonical project; every mutation is atomic."""

    def __init__(self, project: Path) -> None:
        """Open lazily from the room runtime, away from the UI thread."""
        self.project = canonical_project(project)
        self.directory = self.project / ".nova" / "workspace"
        self.directory.mkdir(parents=True, exist_ok=True)
        # Exclude runtime state even in projects without Nova's .gitignore.
        code, common, _ = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], project)
        if code == 0:
            exclude = Path(common) / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            old = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if "/.nova/workspace/" not in old.splitlines():
                with exclude.open("a", encoding="utf-8") as stream:
                    stream.write("\n/.nova/workspace/\n")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.directory / "rooms.sqlite3",
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                author TEXT NOT NULL, text TEXT NOT NULL, job TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, room TEXT NOT NULL,
                body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, room TEXT NOT NULL,
                agent TEXT NOT NULL, task TEXT NOT NULL, prompt TEXT NOT NULL,
                status TEXT NOT NULL, runtime TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                usage TEXT, session TEXT, readonly INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS conversations(
                room TEXT, agent TEXT, task TEXT, session TEXT,
                PRIMARY KEY(room,agent,task));
            CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, room TEXT NOT NULL,
                author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS views(viewer TEXT, room TEXT, draft TEXT DEFAULT '',
                seen INTEGER DEFAULT 0, PRIMARY KEY(viewer,room));
            CREATE TABLE IF NOT EXISTS runtimes(id TEXT PRIMARY KEY, pid INTEGER, born REAL);
        """)
        with self.transaction():
            columns = {r["name"] for r in self.rows("PRAGMA table_info(jobs)")}
            if "readonly" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN readonly INTEGER NOT NULL DEFAULT 0")
            for room, name in [("general", "#general"), *((f"dm:{a}", a.title()) for a in AGENTS)]:
                self.db.execute("INSERT OR IGNORE INTO rooms VALUES(?,?)", (room, name))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize writers across threads and processes with rollback."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def rows(self, sql: str, args: tuple = ()) -> list[Record]:
        """Read a snapshot under the connection lock."""
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    def room(self, room: str, agent: str = "owner") -> Record:
        """Check existence and DM access for a bound participant."""
        rows = self.rows("SELECT * FROM rooms WHERE id=?", (room,))
        if (
            not rows
            or agent not in ("owner", *AGENTS)
            or (room.startswith("dm:") and agent not in ("owner", room[3:]))
        ):
            msg = "Unknown or unauthorized room"
            raise ValueError(msg)
        return rows[0]

    def create_channel(self, name: str) -> str:
        """Create a channel without starting any work."""
        name = name.removeprefix("#")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name):
            msg = "Use 1-40 lowercase letters, numbers, hyphens or underscores"
            raise ValueError(msg)
        with self.transaction():
            if self.rows("SELECT id FROM rooms WHERE id=?", (name,)):
                msg = "Channel already exists"
                raise ValueError(msg)
            self.db.execute("INSERT INTO rooms VALUES(?,?)", (name, "#" + name))
        return name

    def history(self, room: str, agent: str = "owner") -> list[Record]:
        """Read only this room's public transcript, never CLI private history."""
        self.room(room, agent)
        return self.rows("SELECT * FROM messages WHERE room=? ORDER BY id", (room,))

    def message(self, room: str, author: str, text: str, job: str | None = None) -> int:
        """Append attributed content; only the coordinator chooses authors."""
        self.room(room, author if author != "system" else "owner")
        checked_text(text)
        with self.transaction():
            cursor = self.db.execute(
                "INSERT INTO messages(room,author,text,job) VALUES(?,?,?,?)",
                (room, author, text, job),
            )
            assert cursor.lastrowid is not None  # noqa: S101 - Successful INSERT always has an ID.
            return cursor.lastrowid

    def context(self, room: str, agent: str) -> Record:
        """Return a bounded valid shared snapshot, never another room's history."""
        self.room(room, agent)

        def bounded(records: list[Record], field: str, budget: int) -> list[Record]:
            result = []
            for record in records:
                if budget <= 0:
                    break
                item = dict(record)
                item[field] = item[field][-budget:]
                budget -= len(item[field])
                result.append(item)
            return list(reversed(result))

        messages = self.rows(
            "SELECT * FROM messages WHERE room=? ORDER BY id DESC LIMIT 50", (room,)
        )
        notes = self.rows("SELECT * FROM notes WHERE room=? ORDER BY rowid DESC LIMIT 30", (room,))
        return {
            "messages": bounded(messages, "text", 40_000),
            "notes": bounded(notes, "body", 20_000),
        }

    def board(self, room: str, agent: str) -> Record:
        """Read published project memory; private notes stay in their bound DM."""
        self.room(room, agent)
        notes = self.rows(
            "SELECT * FROM notes WHERE room NOT LIKE 'dm:%' OR room=? ORDER BY rowid DESC LIMIT 30",
            (room,),
        )
        for note in notes:
            note["body"] = note["body"][:2000]
        tasks = []
        for row in self.rows(
            "SELECT body FROM tasks WHERE room NOT LIKE 'dm:%' OR room=? "
            "ORDER BY rowid DESC LIMIT 20",
            (room,),
        ):
            task = json.loads(row["body"])
            tasks.append(
                {
                    key: task[key]
                    for key in ("id", "room", "status", "participants", "turns", "limit")
                }
                | {"goal": task["goal"][:2000]}
            )
        return {"tasks": tasks, "notes": notes}

    def task(self, task: str) -> Record:
        """Fetch the durable approved scope and lifecycle."""
        rows = self.rows("SELECT body FROM tasks WHERE id=?", (task,))
        if not rows:
            msg = "Unknown task"
            raise ValueError(msg)
        return json.loads(rows[0]["body"])

    def put_task(self, task: Record) -> None:
        """Persist inside the caller's transaction."""
        self.db.execute(
            "INSERT OR REPLACE INTO tasks VALUES(?,?,?)",
            (task["id"], task["room"], json.dumps(task)),
        )

    def propose(self, room: str, goal: str, participants: list[str]) -> Record:
        """Record an owner proposal; no execution until start."""
        self.room(room)
        checked_text(goal)
        if not participants or set(participants) - set(AGENTS):
            msg = "Select Nova, Claude or Codex"
            raise ValueError(msg)
        for agent in participants:
            self.room(room, agent)
        code, base, _ = _git(["rev-parse", "HEAD"], self.project)
        _, dirty, _ = _git(["status", "--porcelain"], self.project)
        task = {
            "id": uuid.uuid4().hex,
            "room": room,
            "goal": goal,
            "participants": list(dict.fromkeys(participants)),
            "status": "awaiting approval",
            "base": base if code == 0 else None,
            "worktree": None,
            "branch": None,
            "operations": ["read", "edit", "test"],
            "turns": 0,
            "limit": TASK_TURNS,
            "warning": "Uncommitted project changes are absent from the worktree." if dirty else "",
            "error": "",
        }
        with self.transaction():
            self.put_task(task)
        return task

    def enqueue(
        self,
        room: str,
        agent: str,
        prompt: str,
        runtime: str,
        task: str = "",
        *,
        readonly: bool = False,
    ) -> str:
        """Queue a single bounded turn. Does not itself grant execution."""
        self.room(room, agent)
        checked_text(prompt)
        key = uuid.uuid4().hex
        with self.transaction():
            if task:
                scope = self.task(task)
                if scope["room"] != room or agent not in scope["participants"]:
                    msg = "Recipient is outside the approved task"
                    raise ValueError(msg)
                if scope["status"] not in {"queued", "working", "finished", "paused"}:
                    msg = "Task is not approved"
                    raise ValueError(msg)
                if scope["turns"] >= scope["limit"]:
                    msg = "Task turn allowance exhausted; owner must extend it"
                    raise ValueError(msg)
            self.db.execute(
                "INSERT INTO jobs(id,room,agent,task,prompt,status,runtime,readonly) "
                "VALUES(?,?,?,?,?,'queued',?,?)",
                (key, room, agent, task, prompt, runtime, int(readonly)),
            )
        return key

    def claim(self, job: str) -> Record | None:
        """Enforce two workers, one per conversation/task, and task turn limits."""
        with self.transaction():
            row = self.rows("SELECT * FROM jobs WHERE id=?", (job,))[0]
            active = self.rows("SELECT * FROM jobs WHERE status='working'")
            if (
                row["status"] != "queued"
                or len(active) >= MAX_WORKERS
                or any(
                    (j["room"], j["agent"], j["task"]) == (row["room"], row["agent"], row["task"])
                    or (row["task"] and j["task"] == row["task"])
                    for j in active
                )
            ):
                return None
            if row["task"]:
                task = self.task(row["task"])
                if task["status"] in {"paused", "blocked", "integrated"}:
                    return None
                if task["turns"] >= task["limit"]:
                    task["status"] = "paused"
                    self.put_task(task)
                    self.db.execute("UPDATE jobs SET status='paused' WHERE id=?", (job,))
                    return None
                task.update(turns=task["turns"] + 1, status="working")
                self.put_task(task)
            self.db.execute("UPDATE jobs SET status='working' WHERE id=?", (job,))
            row["status"] = "working"
            return row

    def finish(self, job: str, status: str, detail: str = "", usage: Record | None = None) -> None:
        """Finish a turn while preserving work and pending handoffs."""
        with self.transaction():
            row = self.rows("SELECT * FROM jobs WHERE id=?", (job,))[0]
            self.db.execute(
                "UPDATE jobs SET status=?,detail=?,usage=? WHERE id=?",
                (status, detail, json.dumps(usage) if usage else None, job),
            )
            if row["task"]:
                task = self.task(row["task"])
                waiting = self.rows(
                    "SELECT id,status FROM jobs WHERE task=? AND status IN ('queued','paused')",
                    (row["task"],),
                )
                next_status = (
                    "paused" if any(j["status"] == "paused" for j in waiting) else "queued"
                )
                task.update(
                    status=next_status if waiting and status == "finished" else status,
                    error=detail if status == "blocked" else "",
                )
                self.put_task(task)

    def session(self, row: Record, value: str | None = None) -> str | None:
        """Separate resumable conversations by room, participant and task."""
        key = (row["room"], row["agent"], row["task"] + (":review" if row.get("readonly") else ""))
        if value:
            with self.transaction():
                self.db.execute(
                    "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?)", (*key, value)
                )
                self.db.execute("UPDATE jobs SET session=? WHERE id=?", (value, row["id"]))
        rows = self.rows(
            "SELECT session FROM conversations WHERE room=? AND agent=? AND task=?", key
        )
        return rows[0]["session"] if rows else None

    def register(self, runtime: str) -> None:
        """Recover only dead owners; opening another Nova never interrupts live work."""
        import os

        import psutil

        with self.transaction():
            for row in self.rows("SELECT * FROM runtimes"):
                try:
                    alive = psutil.Process(row["pid"]).create_time() == row["born"]
                except psutil.NoSuchProcess:
                    alive = False
                except psutil.AccessDenied:
                    alive = True
                if not alive:
                    for job in self.rows(
                        "SELECT id FROM jobs WHERE runtime=? AND status IN ('working','queued')",
                        (row["id"],),
                    ):
                        self.finish_nested(job["id"])
                    self.db.execute("DELETE FROM runtimes WHERE id=?", (row["id"],))
            self.db.execute(
                "INSERT INTO runtimes VALUES(?,?,?)",
                (runtime, os.getpid(), psutil.Process().create_time()),
            )

    def finish_nested(self, job: str) -> None:
        """Pause an uncertain action inside recovery's transaction."""
        row = self.rows("SELECT task FROM jobs WHERE id=?", (job,))[0]
        self.db.execute(
            "UPDATE jobs SET status='paused',detail='Interrupted; owner resume required' "
            "WHERE id=?",
            (job,),
        )
        if row["task"]:
            task = self.task(row["task"])
            task["status"] = "paused"
            self.put_task(task)

    def publish(self, room: str, author: str, kind: str, body: Record) -> str:
        """Publish explicit shared memory; DMs remain private and unexportable."""
        self.room(room, author)
        if kind not in {"decision", "finding", "handoff"}:
            msg = "Unknown note kind"
            raise ValueError(msg)
        checked_text(json.dumps(body, ensure_ascii=False))
        key = uuid.uuid4().hex
        with self.transaction():
            self.db.execute(
                "INSERT INTO notes VALUES(?,?,?,?,?)",
                (key, room, author, kind, json.dumps(body, ensure_ascii=False)),
            )
        return key

    def handoff(self, row: Record, payload: Record, runtime: str) -> Record:
        """Publish and queue a bounded handoff atomically, including exhausted turns."""
        fields = {"goal", "completed", "files", "checks", "questions", "next_owner"}
        if set(payload) != fields or any(not isinstance(v, str) for v in payload.values()):
            msg = "Handoff requires goal, completed, files, checks, questions and next_owner"
            raise ValueError(msg)
        checked_text(json.dumps(payload, ensure_ascii=False))
        with self.transaction():
            task = self.task(row["task"])
            if payload["goal"] != task["goal"] or payload["next_owner"] not in task["participants"]:
                msg = "Handoff changes approved scope or participants"
                raise ValueError(msg)
            if self.rows(
                "SELECT id FROM jobs WHERE task=? AND status IN ('queued','paused')",
                (task["id"],),
            ):
                msg = "Task already has a pending handoff"
                raise ValueError(msg)
            note, job = uuid.uuid4().hex, uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO notes VALUES(?,?,?,?,?)",
                (
                    note,
                    row["room"],
                    row["agent"],
                    "handoff",
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            remaining = task["limit"] - task["turns"]
            status = "queued" if remaining > 0 else "paused"
            self.db.execute(
                "INSERT INTO jobs(id,room,agent,task,prompt,status,runtime) VALUES(?,?,?,?,?,?,?)",
                (
                    job,
                    row["room"],
                    payload["next_owner"],
                    task["id"],
                    json.dumps(payload, ensure_ascii=False),
                    status,
                    runtime,
                ),
            )
            return {"note": note, "job": job, "remaining": remaining, "status": status}

    def export(self, ids: list[str], destination: Path) -> None:
        """Explicitly export selected channel notes without overwriting files."""
        notes = []
        for key in ids:
            rows = self.rows("SELECT * FROM notes WHERE id=?", (key,))
            if not rows or rows[0]["room"].startswith("dm:"):
                msg = "Only selected channel notes can be exported"
                raise ValueError(msg)
            notes.append(rows[0])
        if not notes:
            msg = "Select at least one note"
            raise ValueError(msg)
        sections = []
        for note in notes:
            body = json.loads(note["body"])
            text = body.get("text") or "\n\n".join(
                f"**{key.replace('_', ' ').title()}:** {value}" for key, value in body.items()
            )
            sections.append(
                f"## {note['kind'].title()} · {note['author']} · #{note['room']}\n\n{text}"
            )
        content = "\n\n".join(sections)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(content + "\n")

    def artifact(self, room: str, author: str, title: str, text: str) -> str:
        """Reuse immutable content-addressed team artifacts under room authority."""
        from novacode_cli.teams.output import persist_output

        self.room(room, author)
        checked_text(title)
        checked_text(text)
        artifact = persist_output(self.directory / "artifacts", text)
        return self.publish(room, author, "finding", {"text": title, "artifact": artifact})

    def read_artifact(self, room: str, author: str, note: str) -> str:
        """Read published channel artifacts or an artifact from the bound DM."""
        import hashlib

        self.room(room, author)
        notes = self.rows("SELECT room,body FROM notes WHERE id=?", (note,))
        if notes and notes[0]["room"].startswith("dm:") and notes[0]["room"] != room:
            msg = "Unknown or unauthorized room artifact"
            raise ValueError(msg)
        artifact = json.loads(notes[0]["body"]).get("artifact") if notes else None
        if not artifact:
            msg = "Unknown or unauthorized room artifact"
            raise ValueError(msg)
        path = Path(artifact["path"])
        if path.resolve().parent != (self.directory / "artifacts").resolve():
            msg = "Artifact is outside room storage"
            raise ValueError(msg)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            msg = "Artifact failed verification; it was preserved"
            raise ValueError(msg)
        return raw.decode("utf-8")

    def view(
        self, viewer: str, room: str, draft: str | None = None, *, seen: bool = False
    ) -> Record:
        """Persist drafts and read positions per Nova session."""
        self.room(room)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO views(viewer,room) VALUES(?,?)", (viewer, room))
            if draft is not None:
                self.db.execute(
                    "UPDATE views SET draft=? WHERE viewer=? AND room=?", (draft, viewer, room)
                )
            if seen:
                self.db.execute(
                    "UPDATE views SET seen=COALESCE((SELECT MAX(id) FROM messages WHERE room=?),0) "
                    "WHERE viewer=? AND room=?",
                    (room, viewer, room),
                )
            return self.rows("SELECT * FROM views WHERE viewer=? AND room=?", (viewer, room))[0]

    def close(self) -> None:
        """Close the connection after its runtime stops."""
        with self.lock:
            self.db.close()
