"""Hide only dedicated Windows consoles belonging to verified Nova QEMU guests.

Cua launches QEMU in native code, outside Python's subprocess flags. Console
attachment therefore happens in a short-lived, windowless helper process, never
in Nova's terminal process. The guest's graphical desktop remains enabled.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


def _owned_process(pid: int, name: str) -> bool:
    if type(pid) is not int or pid <= 0 or not re.fullmatch(r"nova-[0-9a-f]{16}", name):
        return False
    process = psutil.Process(pid)
    arguments = process.cmdline()
    return (
        Path(process.exe()).name.casefold() == "qemu-system-x86_64.exe"
        and "-name" in arguments
        and arguments[arguments.index("-name") + 1 :][:1] == [name]
    )


class _ConsoleAPI:
    def __init__(self) -> None:
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user = ctypes.WinDLL("user32", use_last_error=True)
        self.attach = kernel.AttachConsole
        self.attach.argtypes, self.attach.restype = [wintypes.DWORD], wintypes.BOOL
        self.detach = kernel.FreeConsole
        self.detach.argtypes, self.detach.restype = [], wintypes.BOOL
        self.window = kernel.GetConsoleWindow
        self.window.argtypes, self.window.restype = [], wintypes.HWND
        self._members = kernel.GetConsoleProcessList
        self._members.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        self._members.restype = wintypes.DWORD
        self._show = user.ShowWindow
        self._show.argtypes, self._show.restype = [wintypes.HWND, ctypes.c_int], wintypes.BOOL

    def members(self) -> list[int]:
        buffer = (ctypes.c_ulong * 64)()
        count = self._members(buffer, len(buffer))
        return list(buffer[:count]) if 0 < count <= len(buffer) else []

    def hide(self, window: int) -> None:
        self._show(window, 0)  # SW_HIDE: hide the console, not the QEMU process.


def _hide_console(pid: int, api: Any) -> bool:  # noqa: ANN401 - isolated Win32 API/test boundary.
    """Run only in the helper; never detach or attach Nova's own console."""
    if not api.attach(pid):
        return False
    try:
        # A shared terminal must remain visible, even if QEMU inherited it.
        if set(api.members()) != {pid, os.getpid()}:
            return False
        window = api.window()
        if not window:
            return False
        api.hide(window)
        return True
    finally:
        api.detach()


def _attempt(name: str, seen: set[int]) -> None:
    from novacode_cli.computer.windows_qemu import _saved_state

    try:
        _, state = _saved_state(name)
        pid = state.get("pid")
        if not _owned_process(pid, name) or pid in seen:
            return
        seen.add(pid)
        subprocess.run(  # noqa: S603 - fixed helper module and verified guest identity.
            [sys.executable, "-m", "novacode_cli.computer.windows_console", str(pid), name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return  # The SDK has not written the guest's launch state yet.
    except (OSError, ValueError, psutil.Error, subprocess.SubprocessError):
        # Visibility must not break guest startup, recovery, or cancellation.
        logger.debug("Could not hide Nova guest console", exc_info=True)


async def _monitor(name: str, stop: asyncio.Event, seen: set[int]) -> None:
    while not stop.is_set():
        await asyncio.to_thread(_attempt, name, seen)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.1)
    # Covers a fast resume, and waits for any in-progress helper before exit.
    await asyncio.to_thread(_attempt, name, seen)


@asynccontextmanager
async def background_guest(name: str) -> AsyncIterator[None]:
    """Scope a Windows console watcher to VM creation or retained-guest resume."""
    if sys.platform != "win32":
        yield
        return
    stop = asyncio.Event()
    task = asyncio.create_task(_monitor(name, stop, set()))
    try:
        yield
    finally:
        stop.set()
        await task


def main() -> int:
    """Attach only this helper to a verified guest and hide its dedicated console."""
    if sys.platform != "win32" or len(sys.argv) != 3:  # noqa: PLR2004
        return 1
    try:
        pid, name = int(sys.argv[1]), sys.argv[2]
        if not _owned_process(pid, name):
            return 1
        api = _ConsoleAPI()
        # Windows' venv redirector can give even a CREATE_NO_WINDOW child an
        # inherited console association. Detach this isolated helper first.
        api.detach()
        return 0 if _hide_console(pid, api) else 1
    except (OSError, ValueError, psutil.Error):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
