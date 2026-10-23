"""Detached daemon tool — launch long-lived background processes that survive the CLI.

The ``daemon`` tool starts a process that keeps running after the CLI exits (a
detached daemon), tracks it in a JSON registry under ``~/.nova/daemons/``, and lets
the agent check status, tail logs, and stop it. This is distinct from the
in-session :class:`~novacode_cli.shell.jobs.BackgroundJob`, which dies with the CLI.

Windows-first (per NOVA.md): the child is spawned with ``CREATE_NO_WINDOW |
CREATE_NEW_PROCESS_GROUP`` so it survives the console while still being able to
write to its log — see ``_CREATE_FLAGS`` for why ``DETACHED_PROCESS`` is wrong
here. On POSIX it uses ``start_new_session=True``, making the child a
process-group leader; ``stop`` signals that whole group, because with
``shell=True`` the pid is the shell and the real daemon is its child.
stdout/stderr are redirected to a per-daemon log file.

This module must NEVER ``console.print`` — it runs inside the live agent loop / TUI.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from langchain.tools import tool

from novacode_cli.daemons.registry import (
    DaemonInfo,
    get,
    is_alive,
    is_ours,
    list_daemons,
    log_path_for,
    process_created_at,
    register,
    safe_name,
    unregister,
)

#: Tail at most this much of a log file. Daemon logs are append-only and never
#: rotated, so a days-old server's log is routinely hundreds of MB — reading it
#: whole to show 30 lines would spike memory inside the live agent loop.
_TAIL_MAX_BYTES = 256 * 1024

#: Windows creation flags that run the child without a console window and in its
#: own process group. ``CREATE_NO_WINDOW`` (not ``DETACHED_PROCESS``) is used so
#: the child's stdout/stderr can still be captured to the log file — a
#: ``DETACHED_PROCESS`` has no console and console apps (e.g. ``python``) can't
#: write to stdout when detached, so their output is lost.
_CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)


def _spawn(command: str, cwd: str, log_path: Path) -> int:
    """Spawn *command* detached, redirecting output to *log_path*. Returns the pid."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        if os.name == "nt":
            proc = subprocess.Popen(  # noqa: S602 — the tool's whole job is running shell commands
                command,
                shell=True,
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_FLAGS,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(  # noqa: S602 — the tool's whole job is running shell commands
                command,
                shell=True,
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    return proc.pid


def _tail(path: Path, lines: int = 30) -> str:
    """Return the last *lines* of a log file, or a friendly placeholder."""
    if not path.exists():
        return "(no log file yet)"
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_MAX_BYTES))
            raw = fh.read()
    except OSError:
        return "(log unreadable)"
    text = raw.decode("utf-8", errors="replace")
    if size > _TAIL_MAX_BYTES:
        # The window almost certainly starts mid-line; drop that partial line.
        text = text.split("\n", 1)[-1]
    if not text.strip():
        return "(log is empty)"
    tail = text.splitlines()[-max(1, lines) :]
    return "\n".join(tail)


@tool
def daemon(
    action: str,
    name: str = "",
    command: str = "",
    cwd: str = "",
    tail_lines: int = 30,
) -> str:
    """Manage a detached background daemon that survives the CLI session.

    Use this to launch a long-lived process (a server, watcher, or worker) that
    keeps running after Nova exits, then check/stop it later.

    Args:
        action: "start", "status", "logs", "stop", or "list".
        name: Unique daemon name (required for start/status/logs/stop).
        command: Shell command to run (required for start).
        cwd: Working directory for the daemon (default: current directory).
        tail_lines: How many trailing log lines to show for "logs" (default 30).

    Returns:
        A human-readable result describing what happened.
    """
    action = (action or "").strip().lower()
    name = (name or "").strip()

    if action == "list":
        return _list_daemons()
    if not name:
        return "Error: name is required for start/status/logs/stop."
    handlers = {
        "start": lambda: _start_daemon(name, command, cwd),
        "status": lambda: _status_daemon(name),
        "logs": lambda: _logs_daemon(name, tail_lines),
        "stop": lambda: _stop_daemon(name),
    }
    handler = handlers.get(action)
    if handler is None:
        return f"Error: unknown action '{action}'. Supported: start, status, logs, stop, list."
    return handler()


def _list_daemons() -> str:
    """Render the list of registered daemons."""
    daemons = list_daemons()
    if not daemons:
        return "No daemons registered."
    lines = [f"{len(daemons)} daemon(s):"]
    for d in daemons:
        # is_ours, not is_alive: a recycled pid would be reported as "running".
        state = "running" if is_ours(d) else "stopped"
        lines.append(f"  {d.name}  {state}  pid {d.pid}  - {d.command[:60]}")
    return "\n".join(lines)


def _start_daemon(name: str, command: str, cwd: str) -> str:
    """Start a daemon and register it."""
    if not command.strip():
        return "Error: command is required for start."
    existing = get(name)
    # is_ours, not is_alive: a recycled pid would otherwise read as "already
    # running" and block a legitimate restart forever.
    if existing is not None and is_ours(existing):
        return f"Error: daemon '{name}' is already running (pid {existing.pid})."
    log_path = log_path_for(name)
    try:
        pid = _spawn(command, cwd or str(Path.cwd()), log_path)
    except OSError as exc:
        return f"Error: failed to start daemon: {exc}"
    try:
        register(
            DaemonInfo(
                name=name,
                pid=pid,
                command=command,
                log_path=str(log_path),
                started_at=time.time(),
                cwd=cwd or str(Path.cwd()),
                # Captured now so a later stop can tell this process from
                # whatever inherits its pid.
                create_time=process_created_at(pid) or 0.0,
            )
        )
    except OSError as exc:
        # The process is already running; if we cannot record it, say so rather
        # than reporting success and leaving an untrackable orphan.
        return (
            f"Started daemon '{name}' (pid {pid}) but FAILED to register it: "
            f"{exc}. It is running untracked — stop it manually via pid {pid}."
        )
    note = ""
    if safe_name(name) != name:
        note = f" (log name sanitized to '{safe_name(name)}')"
    return f"Started daemon '{name}' (pid {pid}). Log: {log_path}{note}"


def _status_daemon(name: str) -> str:
    """Report a daemon's running/stopped state."""
    info = get(name)
    if info is None:
        return f"Daemon '{name}' is not registered."
    state = "running" if is_ours(info) else "stopped"
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.started_at))
    return f"Daemon '{name}': {state} (pid {info.pid}, started {started})."


def _logs_daemon(name: str, tail_lines: int) -> str:
    """Tail a daemon's log file."""
    info = get(name)
    if info is None:
        return f"Daemon '{name}' is not registered."
    return _tail(Path(info.log_path), tail_lines)


def _stop_daemon(name: str) -> str:
    """Stop a daemon and unregister it."""
    info = get(name)
    if info is None:
        return f"Daemon '{name}' is not registered."

    if not is_alive(info.pid):
        unregister(name)
        return f"Daemon '{name}' was not running; removed from the registry."

    # Legacy entries and failed OS probes provide liveness, not ownership.
    # Keep them visible, but never force-kill a possibly recycled live PID.
    if info.create_time <= 0 or process_created_at(info.pid) is None:
        return (
            f"Error: cannot verify process identity for daemon '{name}' (pid {info.pid}). "
            "Nothing was killed. Left registered for manual inspection."
        )

    # A live pid is NOT proof it is still our daemon — pids are reused and this
    # registry outlives reboots. Killing on liveness alone would force-kill an
    # unrelated process tree (`taskkill /T /F`) that merely inherited the number.
    if not is_ours(info):
        unregister(name)
        return (
            f"Daemon '{name}' is gone — pid {info.pid} now belongs to a different "
            "process, so nothing was killed. Removed the stale entry."
        )

    try:
        if os.name == "nt":
            from novacode_cli.utils.windows import taskkill_executable

            proc = subprocess.run(  # noqa: S603 — terminating a daemon we started
                [taskkill_executable(), "/PID", str(info.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0 and is_alive(info.pid):
                detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
                return (
                    f"Error: failed to stop daemon '{name}' (pid {info.pid}): "
                    f"{detail or 'taskkill failed'}. Left registered so it can be "
                    "retried."
                )
        else:
            # shell=True means info.pid is the shell; the real daemon is its
            # child. Signal the whole group — which is why the spawn asks for a
            # new session. Falls back to the pid if the group is already gone.
            try:
                os.killpg(os.getpgid(info.pid), 15)  # SIGTERM
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(info.pid, 15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Error: failed to stop daemon '{name}': {exc}"

    # Confirm rather than assume: unregistering a process that is still running
    # makes it permanently untrackable.
    for _ in range(20):
        if not is_alive(info.pid):
            break
        time.sleep(0.05)
    else:
        return (
            f"Signalled daemon '{name}' (pid {info.pid}) but it is still running. "
            "Left registered so it can be stopped again."
        )

    unregister(name)
    return f"Stopped daemon '{name}'."
