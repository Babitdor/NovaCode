"""Terminate subprocess groups owned by Nova, including their descendants."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from typing import Any


def process_group_options() -> dict[str, Any]:
    """Create an isolated group so stopping a command never signals Nova itself."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    """Force-stop an owned group synchronously during process shutdown."""
    try:
        if sys.platform == "win32":
            from novacode_cli.utils.windows import taskkill_executable

            subprocess.run(  # noqa: S603 - fixed taskkill executable and an owned PID
                [taskkill_executable(), "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        else:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass


async def terminate_process_tree(proc: asyncio.subprocess.Process, *, grace: float = 3.0) -> None:
    """Stop an owned process tree without blocking the event loop."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        from novacode_cli.utils.windows import taskkill_executable

        killer = None
        try:
            # Stop descendants before their shell disappears: taskkill cannot
            # discover the tree once the root has already been terminated.
            killer = await asyncio.create_subprocess_exec(
                taskkill_executable(),
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            code = await asyncio.wait_for(killer.wait(), timeout=5)
            if code != 0 and proc.returncode is None:
                proc.kill()
        except (OSError, TimeoutError):
            if killer is not None and killer.returncode is None:
                killer.kill()
                await killer.wait()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return

    try:
        group = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    # Callers must own an isolated group; never signal the host terminal group.
    if group != proc.pid:
        proc.kill() if grace <= 0 else proc.terminate()
        return
    if grace > 0:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=grace)
    # Retain the group id even if its shell exits before a child that ignores TERM.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)
