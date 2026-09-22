"""Background tasks + cooperative control for the foreground shell command.

Two things live here:

1. :class:`ForegroundControl` — the shell/execute tool runs its subprocess inside
   a detached thread with its own event loop (``ShellMiddleware._run_async``), so
   the TUI can't reach it via Textual task cancellation. While a foreground command
   runs, the middleware publishes a control; the TUI sets its events from keybinds
   (Esc → ``kill``, Ctrl+B → ``detach``) and the read loop polls them.

2. :class:`JobRegistry` — the background task manager. A command detached with
   Ctrl+B (or launched in the background) becomes a :class:`BackgroundJob` with a
   bounded live log buffer, a per-job terminate signal, and status transitions
   (running → done | failed | terminated). Observers (the TUI indicator/panel and
   the completion notifier) are fired reactively on every state change.

# ponytail: Esc / Ctrl+B act on EVERY running foreground command (one turn's
# parallel calls). Key controls by tool_call_id if a per-call action is ever needed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Foreground control (Esc kill / Ctrl+B detach)
# ---------------------------------------------------------------------------


@dataclass
class ForegroundControl:
    """Signals the middleware's foreground read loop polls each iteration."""

    command: str
    kill: threading.Event = field(default_factory=threading.Event)
    detach: threading.Event = field(default_factory=threading.Event)


# Every foreground command running right now. A turn's tool calls run
# concurrently, so there can be several: with a single slot the newest call
# overwrote the older ones' control, and Esc / Ctrl+B could no longer reach them
# (an older command then ran to its full timeout: the original freeze).
_live: list[ForegroundControl] = []
_lock = threading.Lock()


def set_current(command: str) -> ForegroundControl:
    """Publish a fresh control for a foreground command about to run."""
    ctl = ForegroundControl(command=command)
    with _lock:
        _live.append(ctl)
    return ctl


def get_current() -> ForegroundControl | None:
    """The most recently started live control, or None if nothing is running."""
    with _lock:
        return _live[-1] if _live else None


def clear_current(ctl: ForegroundControl) -> None:
    """Retire ``ctl`` once its command is done."""
    with _lock:
        if ctl in _live:
            _live.remove(ctl)


def request_kill() -> bool:
    """Ask every running foreground command to die. True if any was running."""
    with _lock:
        live = list(_live)
    for ctl in live:
        ctl.kill.set()
    return bool(live)


def request_detach() -> bool:
    """Move every running foreground command to the background.

    True if at least one was running and not already being killed.
    """
    with _lock:
        live = [c for c in _live if not c.kill.is_set()]
    for ctl in live:
        ctl.detach.set()
    return bool(live)


# ---------------------------------------------------------------------------
# Background jobs — the task manager
# ---------------------------------------------------------------------------

_LOG_MAXLEN = 1000  # bounded live-log buffer (~1000 chunks of <=1KB → ~1MB max)

# Maximum number of completed (non-running) jobs retained in the registry.
# Completed jobs are kept so ``/jobs`` can list and restart recent ones, but an
# unbounded dict leaks memory across a long session. Oldest completed jobs are
# evicted when a new job is added.
_MAX_COMPLETED_JOBS = 50

# event ∈ {started, output, completed, failed, terminated, cleared}; job may be
# None for "cleared".
Observer = Callable[[str, "BackgroundJob | None"], None]


def fmt_runtime(seconds: float) -> str:
    """Format a runtime as MM:SS (or H:MM:SS past an hour)."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


@dataclass
class BackgroundJob:
    """A shell/execute command running (or finished) in the background."""

    id: int
    command: str
    tool_name: str
    started_at: float
    prog: list[str] | None = None
    status: str = "running"  # running | done | failed | terminated
    exit_code: int | None = None
    finished_at: float | None = None
    pid: int | None = None
    resume_on_done: bool = False
    """True for Ctrl+B-detached jobs: the agent was mid-task, so when this
    finishes the TUI auto-resumes the agent with the result (vs. just notifying
    for tasks the user explicitly launched/restarted)."""
    agent_launched: bool = False
    """True when the AGENT started this job (``background=True`` on shell/bash/
    execute), as opposed to the user launching or restarting it.

    Such a job is part of work the agent is actively doing, so it should not sit
    silently until the user happens to type again — the monitor watches these
    and reports the result back into the conversation. A user-launched dev
    server, by contrast, is expected to just keep running."""
    logs: deque = field(default_factory=lambda: deque(maxlen=_LOG_MAXLEN), repr=False)
    kill: threading.Event = field(default_factory=threading.Event, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def task_id(self) -> str:
        return f"task_{self.id}"

    def runtime(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def output(self) -> str:
        return "".join(self.logs)

    def status_glyph(self) -> str:
        return {"running": "●", "done": "✓", "failed": "✗", "terminated": "◼"}.get(self.status, "●")


class JobRegistry:
    """Process-global background task manager."""

    def __init__(self) -> None:
        self._jobs: dict[int, BackgroundJob] = {}
        self._next_id = 41  # cosmetic: task ids start at task_41 like the spec
        self._lock = threading.Lock()
        self._observers: list[Observer] = []
        self._completion_cb: Callable[[BackgroundJob], None] | None = None
        self._launcher: Callable[[str, list | None], BackgroundJob | None] | None = None

    # -- lifecycle ---------------------------------------------------------
    def add(self, command: str, tool_name: str, prog: list | None = None) -> BackgroundJob:
        with self._lock:
            job_id = self._next_id
            self._next_id += 1
            job = BackgroundJob(
                id=job_id,
                command=command,
                tool_name=tool_name,
                started_at=time.time(),
                prog=list(prog) if prog else None,
            )
            self._jobs[job_id] = job
            self._trim_completed_locked()
        self._emit("started", job)
        return job

    def _trim_completed_locked(self) -> None:
        """Drop the oldest completed jobs so ``_jobs`` stays bounded.

        Completed jobs are kept so ``/jobs`` can list and restart recent ones,
        but an unbounded dict leaks memory across a long session. Called under
        ``self._lock`` on each new job; running jobs are never evicted.
        """
        completed = [jid for jid, j in self._jobs.items() if j.status != "running"]
        if len(completed) <= _MAX_COMPLETED_JOBS:
            return
        # Evict oldest first (job ids increase monotonically).
        for jid in sorted(completed)[: len(completed) - _MAX_COMPLETED_JOBS]:
            del self._jobs[jid]

    def append_log(self, job_id: int, text: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        job.logs.append(text)
        self._emit("output", job)

    def attach_pid(self, job_id: int, pid: int | None) -> None:
        job = self.get(job_id)
        if job is not None:
            job.pid = pid

    def complete(self, job_id: int, exit_code: int | None, output: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running":
                return
            if output is not None:
                job.logs.append(output)
            job.exit_code = exit_code
            job.status = "done" if exit_code in (0, None) else "failed"
            job.finished_at = time.time()
            job._done.set()
        self._emit("completed" if job.status == "done" else "failed", job)
        self._fire_completion(job)

    def mark_terminated(self, job_id: int, exit_code: int | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running":
                return
            job.status = "terminated"
            job.exit_code = exit_code
            job.finished_at = time.time()
            job._done.set()
        self._emit("terminated", job)
        self._fire_completion(job)

    # -- actions -----------------------------------------------------------
    def terminate(self, job_id: int) -> bool:
        """Signal a running job to stop (the drain loop terminates its process
        tree gracefully, then marks it terminated). True if it was running."""
        job = self.get(job_id)
        if job is None or job.status != "running":
            return False
        job.kill.set()
        return True

    def restart(self, job_id: int) -> BackgroundJob | None:
        """Re-run a job's command as a new background job (via the launcher)."""
        job = self.get(job_id)
        if job is None or self._launcher is None:
            return None
        return self._launcher(job.command, job.prog)

    def clear_completed(self) -> int:
        with self._lock:
            done = [jid for jid, j in self._jobs.items() if j.status != "running"]
            for jid in done:
                del self._jobs[jid]
        if done:
            self._emit("cleared", None)
        return len(done)

    # -- queries -----------------------------------------------------------
    def get(self, job_id: int) -> BackgroundJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def resolve(self, ref: str | int) -> BackgroundJob | None:
        """Look up a job by numeric id, ``"task_42"``, or ``"42"``."""
        try:
            jid = int(str(ref).lower().replace("task_", "").strip())
        except (ValueError, TypeError):
            return None
        return self.get(jid)

    def list_jobs(self) -> list[BackgroundJob]:
        with self._lock:
            return list(self._jobs.values())

    def active(self) -> list[BackgroundJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status == "running"]

    def active_count(self) -> int:
        return len(self.active())

    def wait(self, job_id: int, timeout: float | None = None) -> BackgroundJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        job._done.wait(timeout)
        return job

    # -- observers ---------------------------------------------------------
    def add_observer(self, cb: Observer) -> None:
        self._observers.append(cb)

    def set_launcher(self, fn: Callable[[str, list | None], BackgroundJob | None]) -> None:
        self._launcher = fn

    def set_completion_callback(self, cb: Callable[[BackgroundJob], None] | None) -> None:
        """Back-compat: called once per job on a terminal transition."""
        self._completion_cb = cb

    def _emit(self, event: str, job: BackgroundJob | None) -> None:
        for cb in list(self._observers):
            try:
                cb(event, job)
            except Exception:  # noqa: BLE001 — a bad observer must not wedge a job
                pass

    def _fire_completion(self, job: BackgroundJob) -> None:
        cb = self._completion_cb
        if cb is not None:
            try:
                cb(job)
            except Exception:  # noqa: BLE001
                pass

    def reset(self) -> None:
        """Drop all jobs, observers, and the completion callback (test isolation)."""
        with self._lock:
            self._jobs.clear()
            self._next_id = 41
        self._observers.clear()
        self._completion_cb = None


_registry: JobRegistry | None = None


def get_registry() -> JobRegistry:
    """Return the process-global background-task registry."""
    global _registry
    if _registry is None:
        _registry = JobRegistry()
        _register_exit_cleanup()
    return _registry


def _kill_pid_tree(pid: int) -> None:
    """Force-kill a process and its children by pid (sync; for atexit)."""
    import os
    import signal
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass


_exit_cleanup_registered = False


def _register_exit_cleanup() -> None:
    """Kill any still-running background task trees when the process exits, so a
    detached `npm run dev` doesn't outlive the assistant (spec: clean up on exit)."""
    global _exit_cleanup_registered
    if _exit_cleanup_registered:
        return
    _exit_cleanup_registered = True
    import atexit

    def _cleanup() -> None:
        reg = _registry
        if reg is None:
            return
        for job in reg.active():
            if job.pid:
                _kill_pid_tree(job.pid)

    atexit.register(_cleanup)


# ---------------------------------------------------------------------------
# Shared background event loop — lets a detached command's drain outlive the
# synchronous tool call that started it (see the middleware for the full why).
# ---------------------------------------------------------------------------

_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_loop_lock = threading.Lock()


def get_background_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background event loop, starting it on first use.

    Runs in a daemon thread (dies with the process). On Windows the default
    policy yields a Proactor loop, which supports subprocesses.
    """
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None or _bg_loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever, name="nova-shell-bg-loop", daemon=True
            ).start()
            _bg_loop = loop
        return _bg_loop


if __name__ == "__main__":
    # ponytail: self-check for the foreground slot + registry state machine.
    assert get_current() is None and request_kill() is False
    c = set_current("sleep 100")
    assert request_kill() is True and c.kill.is_set()
    assert request_detach() is False  # kill wins
    clear_current(c)

    r = JobRegistry()
    events: list[tuple[str, str | None]] = []
    r.add_observer(lambda ev, j: events.append((ev, j.task_id if j else None)))
    j = r.add("npm run dev", "shell")
    assert j.task_id == "task_41" and j.status == "running"
    r.append_log(j.id, "listening on :3000\n")
    assert "listening" in j.output
    assert r.resolve("task_41") is j and r.resolve(41) is j and r.resolve("nope") is None
    assert r.terminate(j.id) is True and j.kill.is_set()
    r.mark_terminated(j.id, exit_code=-15)
    assert j.status == "terminated"
    assert r.terminate(j.id) is False  # not running

    j2 = r.add("pytest", "shell")
    r.complete(j2.id, 1)
    assert j2.status == "failed" and j2.exit_code == 1
    j3 = r.add("build", "shell")
    r.complete(j3.id, 0)
    assert j3.status == "done"
    assert r.active_count() == 0
    assert r.clear_completed() == 3 and r.list_jobs() == []
    assert [e[0] for e in events] == [
        "started", "output", "terminated",
        "started", "failed", "started", "completed", "cleared",
    ]
    assert fmt_runtime(151) == "02:31" and fmt_runtime(3661) == "1:01:01"
    print("shell.jobs self-check ok")
