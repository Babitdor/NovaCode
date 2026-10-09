"""Parent-side manager for spawned Nova session processes.

Owns the child processes behind the TUI's session tabs: launching them in their
worktree, pumping their JSONL stdout onto the app's event loop, routing prompts
and approval decisions back down, and making sure none of them outlive the parent.

Everything runs as asyncio tasks on the app's own loop, so ``on_message`` is
invoked directly without ``call_from_thread`` marshalling.

Two failure modes get explicit handling because both otherwise hang the UI:

* a child that dies mid-approval would leave the parent awaiting a decision that
  can never arrive, so every pending future is resolved on crash;
* ``main.py`` ends in ``os._exit()``, which runs no ``finally`` blocks, so an
  ``atexit`` sweep force-kills surviving children rather than orphaning them.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import itertools
import logging
import os
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novacode_cli.sessions import protocol

logger = logging.getLogger(__name__)

# A single ToolResult line can be large; the default 64 KiB StreamReader limit
# would raise LimitOverrunError and kill the stream mid-turn.
_STREAM_LIMIT = 8 * 1024 * 1024

# Each child is a full Nova (agent build, MCP servers, model client), so this is
# about RAM and concurrent token spend, not a technical ceiling.
MAX_SESSIONS = 4

_SHUTDOWN_GRACE = 10.0

# Live children, for the atexit sweep. Module-global because atexit has no
# access to the app instance.
_live: set[ChildSession] = set()
_atexit_registered = False


@dataclass(eq=False)
class ChildSession:
    """One spawned session and everything the parent knows about it.

    ``eq=False`` keeps identity hashing/equality: these are unique live objects
    held in a set for the atexit sweep, and a mutable dataclass is unhashable.
    """

    session_id: str
    name: str
    worktree: Path
    branch: str | None = None
    proc: Any = None
    status: str = "starting"
    """starting | idle | running | needs-approval | crashed | exited"""
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    tasks: list[asyncio.Task] = field(default_factory=list)
    exit_code: int | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


def _sweep_live_children() -> None:
    """atexit: force-kill any child still running (os._exit skips finally)."""
    from novacode_cli.shell.jobs import _kill_pid_tree

    for child in list(_live):
        proc = child.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                _kill_pid_tree(proc.pid)


class SessionSupervisor:
    """Spawns and talks to child session processes."""

    def __init__(self, on_message: Callable[[str, dict], Awaitable[None]]) -> None:
        """*on_message* is awaited with ``(session_id, message)`` for every frame."""
        self._on_message = on_message
        self._children: dict[str, ChildSession] = {}
        self._starting: set[str] = set()
        self._prompt_ids = itertools.count(1)

        global _atexit_registered
        if not _atexit_registered:
            atexit.register(_sweep_live_children)
            _atexit_registered = True

    # ── accessors ────────────────────────────────────────────────────────

    def get(self, session_id: str) -> ChildSession | None:
        return self._children.get(session_id)

    def list(self) -> list[ChildSession]:
        return list(self._children.values())

    def at_capacity(self) -> bool:
        return sum(c.alive for c in self._children.values()) + len(self._starting) >= MAX_SESSIONS

    # ── spawn ────────────────────────────────────────────────────────────

    @staticmethod
    def _child_env() -> dict[str, str]:
        """Environment that makes the child run THIS Nova, not the worktree's copy.

        The child is launched with ``cwd=<worktree>`` so it resolves its project
        root (and therefore its whole workspace) there. But ``python -m`` also
        puts the CWD first on ``sys.path``, so it would import ``novacode_cli``
        from the worktree — a checkout of HEAD, i.e. a *different, older* Nova.
        That is not theoretical: it made every spawned session die instantly with
        ``error: argument command: invalid choice`` because the worktree's copy
        had no ``--session-worker`` flag.

        The worktree is the agent's workspace, never its implementation. So:
        ``PYTHONSAFEPATH`` stops CWD being prepended (3.11+), and ``PYTHONPATH``
        points at the parent's package root so the import still resolves.
        """
        import novacode_cli

        pkg_root = Path(novacode_cli.__file__).resolve().parent.parent
        existing = os.environ.get("PYTHONPATH", "")
        return {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONSAFEPATH": "1",
            "PYTHONPATH": f"{pkg_root}{os.pathsep}{existing}" if existing else str(pkg_root),
        }

    def _argv(self, session_id: str, assistant_id: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "novacode_cli.main",
            "--session-worker",
            "--session-id",
            session_id,
            "--agent",
            assistant_id,
        ]

    async def spawn(
        self,
        *,
        session_id: str,
        name: str,
        worktree: Path,
        branch: str | None = None,
        assistant_id: str = "nova-agent",
        argv: list[str] | None = None,
    ) -> ChildSession:
        """Launch a child bound to *worktree*.

        The worktree binding is the ``cwd`` alone: Nova resolves its project root
        from the working directory at import time, so the child's whole world is
        scoped there without any extra plumbing.
        """
        if session_id in self._children or session_id in self._starting:
            raise ValueError(f"Session {session_id} already exists")
        if self.at_capacity():
            raise RuntimeError("Session limit reached — close one before starting another.")
        child = ChildSession(
            session_id=session_id, name=name, worktree=Path(worktree), branch=branch
        )
        # Reserve before the first await. Simultaneous launches must not start
        # more full agent/MCP processes than the resource limit permits.
        self._starting.add(session_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *(argv or self._argv(session_id, assistant_id)),
                cwd=str(worktree),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
                env=self._child_env(),
            )
        finally:
            self._starting.discard(session_id)
        child.proc = proc
        self._children[session_id] = child
        _live.add(child)

        child.tasks = [
            asyncio.create_task(self._read_stdout(child)),
            asyncio.create_task(self._read_stderr(child)),
        ]
        return child

    # ── readers ──────────────────────────────────────────────────────────

    async def _read_stdout(self, child: ChildSession) -> None:
        """Decode the child's JSONL and hand each frame to the app."""
        stream = child.proc.stdout
        frames = 0
        yielded_at = time.monotonic()
        try:
            while True:
                try:
                    raw = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # One oversized line must not sever the session.
                    logger.debug("oversized line from %s", child.session_id)
                    continue
                if not raw:
                    break
                msg = protocol.loads(raw.decode("utf-8", "replace"))
                if msg is None:
                    continue
                self._track(child, msg)
                await self._on_message(child.session_id, msg)
                frames += 1
                if frames >= 32 or time.monotonic() - yielded_at >= 0.004:
                    # readline() can finish synchronously while the pipe is
                    # full. Several chatty children must leave time for input,
                    # repaint timers, and the other sessions' readers.
                    await asyncio.sleep(0)
                    frames = 0
                    yielded_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a reader must not take down the app
            logger.debug("stdout reader failed for %s", child.session_id, exc_info=True)
        finally:
            await self._on_exit(child)

    async def _read_stderr(self, child: ChildSession) -> None:
        """Keep the tail of stderr so a crash can be explained."""
        stream = child.proc.stderr
        lines = 0
        with contextlib.suppress(asyncio.CancelledError, Exception):
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                child.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
                lines += 1
                if lines >= 32:
                    await asyncio.sleep(0)
                    lines = 0

    def _track(self, child: ChildSession, msg: dict) -> None:
        """Derive status from the frames already flowing; no extra protocol."""
        kind = msg.get("t")
        if kind == "ready":
            child.status = "idle"
        elif kind == "interrupt":
            child.status = "needs-approval"
            iid = str(msg.get("id"))
            child.pending[iid] = asyncio.get_running_loop().create_future()
        elif kind == "turn_done":
            child.status = "idle"
        elif kind == "ev" and child.status != "needs-approval":
            child.status = "running"

    async def _on_exit(self, child: ChildSession) -> None:
        """Finalize a child whose stdout closed: status, and free every waiter."""
        proc = child.proc
        if proc is not None:
            with contextlib.suppress(Exception):
                child.exit_code = await proc.wait()
        _live.discard(child)

        crashed = bool(child.exit_code)
        child.status = "crashed" if crashed else "exited"

        # Nothing may stay blocked on a process that is gone.
        for fut in list(child.pending.values()):
            if not fut.done():
                fut.set_result(None)
        child.pending.clear()

        if crashed:
            detail = "\n".join(child.stderr_tail) or f"exit code {child.exit_code}"
            with contextlib.suppress(Exception):
                await self._on_message(
                    child.session_id, {"t": "error", "message": detail, "fatal": True}
                )
        with contextlib.suppress(Exception):
            await self._on_message(
                child.session_id,
                {"t": "exited", "code": child.exit_code, "crashed": crashed},
            )

        # Drop the dead child. Only close() used to pop from _children, so a
        # child that exited on its own — crashed on startup, killed externally,
        # or simply finished — stayed in the dict for the life of the parent,
        # holding its process handle, reader tasks and stderr tail. The status
        # has already been reported above, so nothing needs the object after
        # this. at_capacity() filtered on .alive and so still allowed new
        # spawns, which is why this leaked quietly instead of blocking.
        self._reap(child)

    def _reap(self, child: ChildSession) -> None:
        """Forget a finished child and release what it held.

        Does NOT cancel ``child.tasks``: this runs from the stdout reader's own
        ``finally``, so cancelling that set would cancel the caller mid-cleanup.
        Both readers exit on their own once the pipes close; dropping the
        reference is enough for them to be collected.
        """
        self._children.pop(child.session_id, None)
        _live.discard(child)
        child.tasks = []

    # ── sending ──────────────────────────────────────────────────────────

    async def _send(self, child: ChildSession, msg: dict) -> bool:
        if not child.alive or child.proc.stdin is None:
            return False
        try:
            child.proc.stdin.write(protocol.dumps(msg).encode("utf-8"))
            await child.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
            logger.debug("write to %s failed", child.session_id, exc_info=True)
            return False
        return True

    async def send_prompt(self, session_id: str, text: str, *, images=None, auto_approve: bool | None = None) -> str | None:
        """Queue a prompt on the child; returns its id, or None if unreachable."""
        child = self._children.get(session_id)
        if child is None:
            return None
        pid = f"p{next(self._prompt_ids)}"
        payload = {"t": "prompt", "id": pid, "text": text}
        if auto_approve is not None:
            payload["auto_approve"] = auto_approve
        if images:
            payload["images"] = [
                {"base64_data": image.base64_data, "format": image.format, "placeholder": "[image]"}
                for image in images
            ]
        if not await self._send(child, payload):
            return None
        child.status = "running"
        return pid

    async def reply_interrupt(self, session_id: str, interrupt_id: str, result: Any) -> None:
        """Send an approval decision back down and clear the pending marker."""
        child = self._children.get(session_id)
        if child is None:
            return
        fut = child.pending.pop(interrupt_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)
        await self._send(
            child, {"t": "interrupt_reply", "id": interrupt_id, "result": result}
        )
        if child.status == "needs-approval":
            child.status = "running"

    async def cancel(self, session_id: str) -> None:
        child = self._children.get(session_id)
        if child is not None:
            await self._send(child, {"t": "cancel"})

    # ── teardown ─────────────────────────────────────────────────────────

    async def close(self, session_id: str, *, timeout: float = _SHUTDOWN_GRACE) -> ChildSession | None:
        """Ask a child to shut down, escalating to terminate then kill."""
        child = self._children.pop(session_id, None)
        if child is None:
            return None

        await self._send(child, {"t": "shutdown"})
        proc = child.proc
        if proc is not None:
            try:
                child.exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    child.exit_code = await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (TimeoutError, asyncio.TimeoutError):
                    from novacode_cli.shell.jobs import _kill_pid_tree

                    with contextlib.suppress(Exception):
                        _kill_pid_tree(proc.pid)

        for task in child.tasks:
            task.cancel()
        for task in child.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        _live.discard(child)
        if child.status not in ("crashed",):
            child.status = "exited"
        return child

    async def close_all(self, *, timeout: float = _SHUTDOWN_GRACE) -> list[ChildSession]:
        """Shut every child down, in parallel."""
        results = await asyncio.gather(
            *(self.close(sid, timeout=timeout) for sid in list(self._children)),
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, ChildSession)]
