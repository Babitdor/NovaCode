"""Agent tools for background tasks.

A shell/execute command detached with Ctrl+B (or restarted from the Background
Tasks panel) keeps running as a background task with a live log buffer. These
tools let the agent see what is running and act on it — so it can answer "is the
dev server still running?", tail logs, stop a task, or restart one.
"""

from __future__ import annotations

from langchain.tools import tool


def _reg():
    from novacode_cli.shell.jobs import get_registry

    return get_registry()


def _fmt(seconds: float) -> str:
    from novacode_cli.shell.jobs import fmt_runtime

    return fmt_runtime(seconds)


@tool
def list_background_tasks() -> str:
    """List background tasks with their status, runtime, and command.

    Use to see what long-running commands are active (e.g. dev servers, watchers)
    or recently finished in THIS session. Task IDs belong to one session/tab;
    another tab's same-numbered task is unrelated. To stop a listed command,
    call terminate_task with its task_id, then check get_task_status.
    """
    tasks = _reg().list_jobs()
    if not tasks:
        return "No background tasks."
    lines = [f"{len(tasks)} background task(s):"]
    for t in tasks:
        state = t.status + (f" (exit {t.exit_code})" if t.status in ("done", "failed") else "")
        lines.append(f"  {t.task_id}  {state}  {_fmt(t.runtime())}  - {t.command[:70]}")
    return "\n".join(lines)


@tool
def get_task_status(task_id: str) -> str:
    """Get a background task's status, runtime, exit code, and command.

    Args:
        task_id: e.g. "task_42" (or just the number).
    """
    t = _reg().resolve(task_id)
    if t is None:
        return f"No background task '{task_id}'. Use list_background_tasks() to see current tasks."
    parts = [f"{t.task_id}: {t.status}", f"runtime {_fmt(t.runtime())}", f"command: {t.command}"]
    if t.exit_code is not None:
        parts.insert(1, f"exit {t.exit_code}")
    return " · ".join(parts)


@tool
def get_task_logs(task_id: str, tail_lines: int = 50) -> str:
    """Return the most recent output lines from a background task.

    Args:
        task_id: e.g. "task_42".
        tail_lines: How many trailing lines to return (default 50). The buffer is
            bounded, so very old output may already have been dropped.
    """
    t = _reg().resolve(task_id)
    if t is None:
        return f"No background task '{task_id}'."
    text = t.output
    if not text.strip():
        return f"{t.task_id} has produced no output yet."
    lines = text.splitlines()
    tail = lines[-max(1, tail_lines):]
    header = f"{t.task_id} ({t.status}) — last {len(tail)} line(s):"
    return header + "\n" + "\n".join(tail)


@tool
def terminate_task(task_id: str) -> str:
    """Stop a background command and its child processes in this session.

    Use list_background_tasks to find its task_id. This signals termination;
    use get_task_status afterwards to confirm it reached "terminated".
    Windows stops the process tree forcibly; POSIX first allows graceful exit.

    Args:
        task_id: e.g. "task_42".
    """
    reg = _reg()
    t = reg.resolve(task_id)
    if t is None:
        return f"No background task '{task_id}'."
    if not reg.terminate(t.id):
        return f"{t.task_id} is not running (status: {t.status})."
    return f"Terminating {t.task_id} ({t.command[:60]}). It will report as terminated shortly."


@tool
def restart_task(task_id: str) -> str:
    """Re-run a background task's command as a NEW background task.

    Args:
        task_id: e.g. "task_42".
    """
    reg = _reg()
    t = reg.resolve(task_id)
    if t is None:
        return f"No background task '{task_id}'."
    new = reg.restart(t.id)
    if new is None:
        return f"Could not restart {t.task_id} (no launcher available)."
    return f"Restarted as {new.task_id}: {new.command[:60]}"


# NOTE: there is intentionally NO blocking "wait_for_job" tool. Background tasks
# are fire-and-forget: keep working and check progress with the non-blocking
# get_task_status / get_task_logs, or terminate_task to stop one. A Ctrl+B-
# detached task auto-resumes the agent with its result when it finishes, so the
# agent never needs to block a turn waiting.


__all__ = [
    "get_task_logs",
    "get_task_status",
    "list_background_tasks",
    "restart_task",
    "terminate_task",
]
