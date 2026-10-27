"""Foreground coordinator: owner decisions, scheduling and bound participant tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from novacode_cli.rooms import cli, workspace
from novacode_cli.rooms.store import TASK_TURNS, Record, RoomStore, recipients


class RoomRuntime:
    """No detached workers; SQLite arbitrates limits across live Nova instances."""

    def __init__(self, store: RoomStore) -> None:
        """Construct without executing any agents."""
        self.store = store
        self.id = uuid.uuid4().hex
        self.running: dict[str, asyncio.Task] = {}
        self.clients: set[asyncio.Task] = set()
        self.bindings: dict[str, Record] = {}
        self.server: asyncio.Server | None = None
        self.pump: asyncio.Task | None = None
        self.closed = False
        self.error = ""

    async def db(self, method: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - Store dispatcher.
        """Keep persistence and Git off the terminal event loop."""
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    async def open(self) -> None:
        """Recover interrupted work as paused, then start an idle scheduler."""
        await self.db("register", self.id)
        self.server = await asyncio.start_server(self._connection, "127.0.0.1", 0, limit=1_000_000)
        self.pump = asyncio.create_task(self._schedule())

    async def send(self, room: str, text: str) -> None:
        """Only an owner composer dispatches discussion mentions."""
        await self.db("message", room, "owner", text)
        for agent in recipients(text, room):
            await self.db("enqueue", room, agent, text, self.id)

    async def start(self, key: str) -> None:
        """Start the exact task card already reviewed by the owner."""

        def approve() -> Record:
            with self.store.transaction():
                task = self.store.task(key)
                if task["status"] != "awaiting approval" or not task["worktree"]:
                    msg = "Prepare and review the worktree before starting"
                    raise ValueError(msg)
                if workspace.git(Path(task["worktree"]), "rev-parse", "HEAD") != task["base"]:
                    msg = "Worktree base changed; create a fresh task"
                    raise ValueError(msg)
                task["status"] = "queued"
                self.store.put_task(task)
                self.store.db.execute(
                    "INSERT INTO jobs(id,room,agent,task,prompt,status,runtime) "
                    "VALUES(?,?,?,?,?,'queued',?)",
                    (
                        uuid.uuid4().hex,
                        task["room"],
                        task["participants"][0],
                        key,
                        task["goal"],
                        self.id,
                    ),
                )
                return task

        await asyncio.to_thread(approve)

    async def resume(self, job: str, *, extend: int = 0) -> None:
        """Owner explicitly acknowledges uncertain work and grants more turns."""

        def update() -> None:
            with self.store.transaction():
                rows = self.store.rows("SELECT * FROM jobs WHERE id=?", (job,))
                if not rows or rows[0]["status"] not in {"paused", "blocked"}:
                    msg = "Only paused or blocked jobs can resume"
                    raise ValueError(msg)
                row = rows[0]
                if row["task"]:
                    task = self.store.task(row["task"])
                    if task["status"] == "integrated":
                        msg = "Integrated tasks cannot resume; create a new task"
                        raise ValueError(msg)
                    if extend < 0 or extend > TASK_TURNS:
                        msg = "Extend by at most six turns per owner decision"
                        raise ValueError(msg)
                    task["limit"] += extend
                    if task["turns"] >= task["limit"]:
                        msg = "Extend the exhausted allowance before resuming"
                        raise ValueError(msg)
                    task["status"] = "queued"
                    self.store.put_task(task)
                self.store.db.execute(
                    "UPDATE jobs SET runtime=?,status='queued' WHERE id=?", (self.id, job)
                )

        await asyncio.to_thread(update)

    async def cancel(self, job: str) -> None:
        """Cancel owned execution and retain the resumable conversation/worktree."""
        rows = await self.db("rows", "SELECT * FROM jobs WHERE id=?", (job,))
        if not rows or rows[0]["runtime"] != self.id:
            msg = "Cancel this job from its running Nova session"
            raise ValueError(msg)
        if rows[0]["status"] in {"queued", "working"}:
            await self.db("finish", job, "paused", "Cancelled by owner")
        active = self.running.get(job)
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

    async def _schedule(self) -> None:
        while not self.closed:
            try:
                await self._dispatch_queued()
                self.error = ""
            except sqlite3.OperationalError as exc:
                # A competing Git preparation can hold the writer transaction.
                # Retrying an atomic claim cannot replay an external action.
                self.error = f"Workspace database unavailable; retrying: {exc}"
            await asyncio.sleep(0.2)

    async def _dispatch_queued(self) -> None:
        rows = await self.db(
            "rows",
            "SELECT id FROM jobs WHERE runtime=? AND status='queued' ORDER BY rowid",
            (self.id,),
        )
        for row in rows:
            if row["id"] not in self.running:
                claimed = await self.db("claim", row["id"])
                if claimed:
                    key = row["id"]
                    task = asyncio.create_task(self._execute(claimed))
                    self.running[key] = task

                    def completed(_: asyncio.Task, key: str = key) -> None:
                        self.running.pop(key, None)

                    task.add_done_callback(completed)

    async def _execute(self, row: Record) -> None:
        token = secrets.token_urlsafe(32)
        self.bindings[token] = row
        try:
            current = await self.db("rows", "SELECT status FROM jobs WHERE id=?", (row["id"],))
            if not current or current[0]["status"] != "working":
                return
            task = await self.db("task", row["task"]) if row["task"] else None
            cwd = Path(task["worktree"]) if task else self.store.project
            context = await self.db("context", row["room"], row["agent"])
            review_diff = None
            if task and row.get("readonly"):
                review_diff = await asyncio.to_thread(workspace.diff, self.store, task["id"])
            prompt = json.dumps(
                {
                    "approved_task": task,
                    "request": row["prompt"],
                    "shared_messages": context["messages"],
                    "published_notes": context["notes"],
                    "review_diff": review_diff,
                },
                ensure_ascii=False,
            )
            session = await self.db("session", row)
            if row["agent"] == "nova":
                from novacode_cli.rooms.nova import run

                turn = await run(self, row, prompt, cwd, session)
            else:
                assert self.server is not None  # noqa: S101 - Scheduler starts after server binding.
                port = self.server.sockets[0].getsockname()[1]

                def save_session(value: str) -> None:
                    self.store.session(row, value)

                turn = await cli.run(
                    row["agent"],
                    prompt,
                    cwd,
                    session,
                    implementation=bool(task) and not row.get("readonly"),
                    bridge={"NOVA_ROOMS_PORT": str(port), "NOVA_ROOMS_TOKEN": token},
                    on_session=save_session,
                )
            if turn.session:
                await self.db("session", row, turn.session)
            if turn.text:
                await self.db("message", row["room"], row["agent"], turn.text, row["id"])
            if turn.error:
                await self.db("message", row["room"], "system", turn.error, row["id"])
            await self.db(
                "finish",
                row["id"],
                "blocked" if turn.error else "finished",
                "\n\n".join(([turn.error] if turn.error else []) + turn.details),
                turn.usage,
            )
        except asyncio.CancelledError:
            await self.db("finish", row["id"], "paused", "Interrupted; owner resume required")
            raise
        except Exception as exc:  # noqa: BLE001 - Provider/process boundary must retain blockers.
            await self.db(
                "message", row["room"], "system", str(exc) or type(exc).__name__, row["id"]
            )
            await self.db("finish", row["id"], "blocked", str(exc))
        finally:
            self.bindings.pop(token, None)

    async def tool(self, row: Record, operation: str, payload: Record) -> Record:
        """Bind author, room and scope to the running job, never tool arguments."""
        current = await self.db(
            "rows", "SELECT status FROM jobs WHERE id=? AND runtime=?", (row["id"], self.id)
        )
        if not current or current[0]["status"] != "working":
            msg = "Participant is not running"
            raise ValueError(msg)
        room, agent = row["room"], row["agent"]
        if operation == "read":
            return await self.db("context", room, agent)
        if operation == "board":
            return await self.db("board", room, agent)
        if operation == "artifact":
            return {"id": await self.db("artifact", room, agent, payload["title"], payload["text"])}
        if operation == "read_artifact":
            return {"text": await self.db("read_artifact", room, agent, payload["note"])}
        if operation == "publish":
            if payload["kind"] not in {"decision", "finding"}:
                msg = "Publish a decision or finding; use structured handoff for tasks"
                raise ValueError(msg)
            return {
                "id": await self.db(
                    "publish", room, agent, payload["kind"], {"text": payload["text"]}
                )
            }
        if operation != "handoff" or not row["task"]:
            msg = "Only approved tasks can hand off; owner controls are unavailable"
            raise ValueError(msg)
        return await self.db("handoff", row, payload, self.id)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = asyncio.current_task()
        if client:
            self.clients.add(client)
        try:
            try:
                data = json.loads(await asyncio.wait_for(reader.readline(), timeout=10))
                token = data.get("token") if isinstance(data, dict) else None
                row = self.bindings.get(token) if isinstance(token, str) else None
                if row is None:
                    msg = "Unauthorized participant"
                    raise ValueError(msg)  # noqa: TRY301 - Uniform protocol error response.
                result = await self.tool(row, data["operation"], data.get("payload", {}))
            except (ValueError, KeyError, TypeError, TimeoutError, OSError) as exc:
                result = {"error": str(exc)}
            writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        finally:
            if client:
                self.clients.discard(client)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def close(self) -> None:
        """Pause owned queued/running turns; never delete task worktrees."""
        self.closed = True
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        rows = await self.db(
            "rows",
            "SELECT id FROM jobs WHERE runtime=? AND status IN ('queued','working')",
            (self.id,),
        )
        for row in rows:
            await self.db("finish", row["id"], "paused", "Nova closed; owner resume required")
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        clients = list(self.clients)
        for client in clients:
            client.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        await self.db("close")
