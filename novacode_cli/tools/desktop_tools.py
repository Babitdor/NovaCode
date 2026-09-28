"""Desktop power tool for the agent: put the machine to sleep.

One tool, one job. It is a tool of its own rather than a `shell` command so the
intent is legible in the transcript and in an audit, and so the "only when the
user is done for the night" policy has a single place to live.

The suspend is spawned **detached** (like :mod:`daemon_tool`), not run
synchronously: a synchronous call blocks until the machine wakes, so the agent's
turn would only finish the next morning and any reply written after the call would
be lost. Detached also means the machine still sleeps if the CLI exits first.

This module must NEVER ``console.print`` — it runs inside the live agent loop / TUI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from langchain.tools import tool

#: Local hours in which an unattended suspend is allowed. Outside it the tool
#: refuses unless the caller passes ``force=True``: parking a desktop somebody is
#: using is much worse than leaving it awake a few hours longer.
OVERNIGHT_START_HOUR = 23
OVERNIGHT_END_HOUR = 7

#: Default grace period between scheduling a suspend and it happening. Long enough
#: for the reply that announced it to be read, short enough to feel immediate.
DEFAULT_DELAY_SECONDS = 20

#: Windows creation flags that keep the child alive after this process exits,
#: without it flashing a console. Same pair the daemon tool uses, and for the same
#: reason: ``DETACHED_PROCESS`` would leave a console app unable to write its log.
_CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)


def _in_overnight_window(hour: int) -> bool:
    """Whether *hour* (local, 0-23) falls inside the overnight window.

    The window normally wraps midnight (23 -> 7), so it is "at or after the start,
    or before the end" rather than a plain range.
    """
    if OVERNIGHT_START_HOUR <= OVERNIGHT_END_HOUR:
        return OVERNIGHT_START_HOUR <= hour < OVERNIGHT_END_HOUR
    return hour >= OVERNIGHT_START_HOUR or hour < OVERNIGHT_END_HOUR


def _suspend_command(
    delay_seconds: int,
    *,
    on_windows: bool | None = None,
    platform: str | None = None,
) -> list[str] | None:
    """The command that suspends this platform, or ``None`` where there is none.

    Windows uses the WinForms call, not the usual
    ``rundll32 powrprof.dll,SetSuspendState`` one-liner: that one hibernates
    whenever hibernation is enabled, which loses the session instead of parking it
    in RAM. The child prints whether the request was accepted, so the log says what
    actually happened.

    Args:
        delay_seconds: Seconds to wait before suspending; 0 suspends at once.
        on_windows: Override the host check (tests only).
        platform: Override ``sys.platform`` (tests only).

    Returns:
        The argv to run, or ``None`` on an unsupported platform.
    """
    delay = max(0, int(delay_seconds))
    windows = os.name == "nt" if on_windows is None else on_windows
    target = sys.platform if platform is None else platform
    if windows:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$ok = [System.Windows.Forms.Application]::SetSuspendState("
            "[System.Windows.Forms.PowerState]::Suspend, $false, $false); "
            'if ($ok) { "suspended" } else { "the suspend request was refused" }'
        )
        if delay:
            script = f"Start-Sleep -Seconds {delay}; {script}"
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    if target == "darwin":
        return ["sh", "-c", f"sleep {delay}; pmset sleepnow"]
    if target.startswith("linux"):
        return ["sh", "-c", f"sleep {delay}; systemctl suspend"]
    return None


def _log_path() -> Path:
    """Where the detached child records whether the suspend was accepted."""
    from novacode_cli.config.config import HOME_DIR

    return Path(HOME_DIR) / "desktop_sleep.log"


def _spawn(command: list[str], log_path: Path) -> int:
    """Start *command* detached, appending its output to *log_path*. Returns the pid.

    Split out from the tool so a test can assert what would run without a test run
    ever putting the developer's machine to sleep.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(  # noqa: S603 — the argv is built here, never from the model
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_FLAGS if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    return proc.pid


@tool
def sleep_desktop(
    reason: str = "",
    *,
    force: bool = False,
    delay_seconds: int = DEFAULT_DELAY_SECONDS,
) -> str:
    """Put this desktop to sleep (suspend to RAM, not hibernate and not shutdown).

    This is how an overnight session ends: the user works late with Nova and wants
    the machine parked rather than left running until morning. Call it as the last
    thing you do, and say in your reply that you are doing it, because the screen
    goes dark a few seconds later. Pass a ``reason`` so the transcript records why.

    Outside the overnight window (23:00-07:00 local) it refuses unless ``force`` is
    set, so nothing can park a desktop somebody is using in the middle of the day.
    Set ``force=True`` only when the user asked for it in so many words.

    Args:
        reason: One short line saying why, e.g. "wrapping up an overnight session".
        force: Allow a suspend outside the overnight window.
        delay_seconds: Grace period before the machine sleeps, so the reply that
            announced it is readable. 0 suspends immediately.

    Returns:
        What was scheduled, phrased for the transcript. The suspend runs in a
        detached child, so a refusal can only be reported later, in the log named
        in the result.
    """
    # Local wall clock, made timezone-aware: the window is about the hour the user
    # sees on their own clock, not UTC.
    now = datetime.now().astimezone()
    if not force and not _in_overnight_window(now.hour):
        return (
            f"Not sleeping the desktop: it is {now:%H:%M} local and the overnight "
            f"window is {OVERNIGHT_START_HOUR:02d}:00-{OVERNIGHT_END_HOUR:02d}:00. "
            "Call it again with force=True if the user asked for it anyway."
        )

    command = _suspend_command(delay_seconds)
    if command is None:
        return f"Not sleeping the desktop: no suspend command for {sys.platform}."

    log_path = _log_path()
    try:
        pid = _spawn(command, log_path)
    except Exception as exc:  # noqa: BLE001 — a failed suspend must not break the turn
        return f"Failed to schedule the suspend: {exc}"

    why = f" ({reason})" if reason else ""
    return (
        f"Desktop will suspend in {max(0, int(delay_seconds))}s{why} "
        f"[pid {pid}]. Abort within the window with: taskkill /PID {pid} /F. "
        f"Outcome is written to {log_path}."
    )
