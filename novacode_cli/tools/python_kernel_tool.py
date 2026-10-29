"""Persistent Python kernel tool — run Python with a namespace that survives calls.

The ``python_kernel`` tool gives the agent a long-lived Python interpreter. State
built up in one call (imports, variables, dataframes) is visible in the next, so
multi-step data work doesn't re-run everything each time. It also supports
dill-based snapshots: ``snapshot="save"`` checkpoints the namespace to a file, and
``snapshot="load:<path>"`` restores it — surviving kernel restarts and letting the
agent persist intermediate state.

The kernel runs as a child subprocess (``python -m novacode_cli.tools.python_kernel_loop``)
with the same privileges as the shell tool. A per-call timeout kills a hung kernel
and respawns it on the next call, so a runaway ``time.sleep(999)`` can't wedge the
agent.

This module must NEVER ``console.print`` — it runs inside the live agent loop / TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from langchain.tools import tool

from novacode_cli.session.runtime_identity import current_identity

logger = logging.getLogger("nova.tools.python_kernel")

#: Per-call timeout (seconds). A hung kernel is killed and respawned.
_DEFAULT_TIMEOUT = 30.0
#: Cap on captured output per call.
_MAX_OUTPUT = 100_000
#: Default snapshot filename (relative to the kernel's cwd).
_DEFAULT_SNAPSHOT = "kernel_snapshot.pkl"

_kernel: _KernelProcess | None = None
_kernels: dict[str, _KernelProcess] = {}
_kernel_lock = threading.Lock()


class _KernelProcess:
    """A managed kernel subprocess with a persistent namespace."""

    def __init__(self, cwd: str | None = None) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._cwd = cwd or str(Path.cwd())
        self._request_lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._active_request: asyncio.Task[Any] | None = None
        self._closed = False

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        # Spawn the kernel loop. Use the same interpreter that's running us.
        python = sys.executable
        cmd = [python, "-u", "-m", "novacode_cli.tools.python_kernel_loop"]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=os.environ.copy(),
        )
        return self._proc

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(ProcessLookupError, ChildProcessError):
            await proc.wait()

    async def request(self, payload: dict[str, Any], timeout_secs: float) -> dict[str, Any]:
        """Serialize exchanges; cancellation cannot leave an unread response."""
        if self._loop is not asyncio.get_running_loop():
            message = "A live Python kernel cannot be shared across event loops."
            raise RuntimeError(message)
        async with self._request_lock:
            if self._closed:
                message = "Python kernel is closed."
                raise RuntimeError(message)
            self._active_request = asyncio.current_task()
            try:
                return await self._request(payload, timeout_secs)
            except BaseException:
                await self._kill()
                raise
            finally:
                self._active_request = None

    async def _request(self, payload: dict[str, Any], timeout_secs: float) -> dict[str, Any]:
        """Send one request while holding exclusive ownership of the pipes."""
        proc = await self._ensure()
        if proc.stdin is None or proc.stdout is None:
            return {"ok": False, "output": "", "error": "kernel pipes unavailable"}
        line = json.dumps(payload) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_secs)
        except TimeoutError:
            logger.debug("python_kernel timed out; killing and respawning")
            await self._kill()
            return {
                "ok": False,
                "output": "",
                "error": f"kernel timed out after {timeout_secs:.0f}s (killed and respawned)",
            }
        if not raw:
            # EOF — the kernel died. Capture stderr for diagnostics.
            stderr = b""
            if proc.stderr is not None:
                try:
                    stderr = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                except (TimeoutError, Exception):  # noqa: BLE001
                    stderr = b""
            await self._kill()
            return {
                "ok": False,
                "output": "",
                "error": f"kernel exited unexpectedly: {stderr.decode('utf-8', 'replace')[:500]}",
            }
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            await self._kill()
            return {"ok": False, "output": "", "error": "kernel returned invalid JSON"}

    async def close(self) -> None:
        self._closed = True
        active = self._active_request
        if active is not None and active is not asyncio.current_task():
            active.cancel()
        async with self._request_lock:
            await self._kill()


async def _get_kernel() -> _KernelProcess:
    global _kernel  # noqa: PLW0603 — module-level singleton kernel process
    with _kernel_lock:
        identity = current_identity.get()
        if identity is not None:
            existing = _kernels.get(identity.session)
            if (
                existing is None
                or existing._closed
                or (existing._proc is None and existing._loop is not asyncio.get_running_loop())
            ):
                _kernels[identity.session] = _KernelProcess(identity.project)
            return _kernels[identity.session]
        if (
            _kernel is None
            or _kernel._closed
            or (_kernel._proc is None and _kernel._loop is not asyncio.get_running_loop())
        ):
            _kernel = _KernelProcess()
        return _kernel


async def release_session_kernel(session: str) -> None:
    """Remove and close only the kernel owned by the ending session."""
    with _kernel_lock:
        kernel = _kernels.pop(session, None)
    if kernel is not None:
        await kernel.close()


@tool
async def python_kernel(
    code: str,
    snapshot: str = "",
    snapshot_path: str = "",
    timeout: float = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — agent-facing per-call timeout
) -> str:
    """Run Python code in a persistent kernel whose namespace survives across calls.

    Use this for multi-step data work: define variables/imports in one call and
    reference them in the next. The kernel keeps running between calls.

    Args:
        code: Python code to execute. State (imports, variables) persists.
        snapshot: Optional. "" (default) runs in the live namespace. "save"
            checkpoints the namespace to a dill file after running. "load:<path>"
            restores a previously saved namespace before running.
        snapshot_path: Filename for "save" (default "kernel_snapshot.pkl").
        timeout: Max seconds for this call (default 30). A hung kernel is killed
            and respawned on the next call.

    Returns:
        The captured stdout/stderr, or an error traceback. Use "reset" as the
        code to clear the namespace.
    """
    if code.strip() == "reset":
        payload: dict[str, Any] = {"op": "reset"}
    else:
        payload = {
            "op": "exec",
            "code": code,
            "snapshot": snapshot or None,
            "snapshot_path": snapshot_path or _DEFAULT_SNAPSHOT,
        }
    kernel = await _get_kernel()
    resp = await kernel.request(payload, timeout)
    output = str(resp.get("output") or "")
    if resp.get("ok"):
        if resp.get("snapshot_path"):
            return f"snapshot saved to {resp['snapshot_path']}\n{output}".rstrip()
        return output.rstrip() or "(no output)"
    error = str(resp.get("error") or "unknown error")
    return f"Error:\n{error}\n{output}".rstrip()
