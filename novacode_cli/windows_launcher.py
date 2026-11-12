"""Keep the invoking Windows shell waiting across clean updater handoffs.

This module must stay stdlib-only. The launcher remains alive while the runtime
exits to release native extensions, then waits for its updater/restarted runtime.
It never reads terminal input or imports the agent environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

OWNER_FILE_ENV = "NOVA_TERMINAL_OWNER_FILE"
CHILD_ENV = "NOVA_TERMINAL_RUNTIME_CHILD"
_WAIT_TIMEOUT = 258


def supervise(arguments: list[str]) -> int | None:
    """Wrap interactive Windows launches; runtime children and pipe modes bypass it."""
    child = os.environ.pop(CHILD_ENV, "") == "1"
    if sys.platform != "win32" or child:
        return None
    if any(
        arg in {"-p", "--print", "--headless", "--session-worker"}
        or arg.startswith(("--print=", "-p"))
        for arg in arguments
    ):
        return None
    if "--mode=pipe" in arguments or any(
        arg == "--mode" and arguments[index + 1 : index + 2] == ["pipe"]
        for index, arg in enumerate(arguments)
    ):
        return None
    if os.environ.get("NOVA_UPDATE_HANDOFF_PID") and not os.environ.get(OWNER_FILE_ENV):
        # An older runtime has already released the shell. Starting another
        # TUI here would compete with its input; keep recovery in this terminal.
        command = subprocess.list2cmdline(["nova", *arguments])
        sys.stdout.write(
            f"The previous launcher has exited. Resume in this terminal with:\n\n  {command}\n\n"
        )
        sys.stdout.flush()
        return 0
    descriptor, filename = tempfile.mkstemp(prefix="nova-terminal-", suffix=".pid")
    os.close(descriptor)
    control = Path(filename)
    env = dict(os.environ)
    env[OWNER_FILE_ENV] = filename
    env[CHILD_ENV] = "1"
    try:
        runtime = subprocess.Popen(  # noqa: S603 - argv only, no shell
            [sys.executable, "-P", "-m", "novacode_cli", *arguments],
            env=env,
            creationflags=0,
        )
        while True:
            try:
                code = runtime.wait()
                break
            except KeyboardInterrupt:
                # Console Ctrl+C reaches the runtime too; let it own cancellation.
                continue
        previous = 0
        while True:
            try:
                pid = int(control.read_text(encoding="ascii").strip() or "0")
            except (OSError, ValueError):
                break
            if pid <= 0 or pid in {previous, os.getpid()}:
                break
            previous = pid
            code = _wait_for_process(pid)
            # A second update publishes its PID before the previous runtime
            # exits. Its waiting updater therefore exits only after publication.
        return code
    finally:
        with suppress(OSError):
            control.unlink(missing_ok=True)


def publish_handoff(pid: int) -> None:
    """Publish the fresh updater before the old runtime releases the shell chain."""
    filename = os.environ.get(OWNER_FILE_ENV)
    if filename:
        Path(filename).write_text(str(pid), encoding="ascii")


def _wait_for_process(pid: int) -> int:
    """Wait for an updater and relay its exit code without reading console input."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000 | 0x1000, wintypes.BOOL(0), pid)
    if not handle:
        # A fast updater may already have exited before its PID is read.
        return 0
    try:
        while True:
            try:
                result = kernel.WaitForSingleObject(handle, 100)
                if result == 0:
                    code = wintypes.DWORD()
                    if kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                        return code.value
                    return 1
                if result != _WAIT_TIMEOUT:
                    return 1
            except KeyboardInterrupt:
                continue
    finally:
        kernel.CloseHandle(handle)
