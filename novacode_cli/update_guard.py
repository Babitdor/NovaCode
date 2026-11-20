"""Windows installation ownership and read-only native file-lock inspection.

Keep this module stdlib-only: importing psutil here would itself map a package
extension that the clean updater must replace.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class UpdateBlockedError(ValueError):
    """Updating cannot safely begin; no package changes have been attempted."""


def _blocked_message(exclusive: bool) -> str:
    if exclusive:
        return (
            "Other Nova sessions or an update are using this installation. "
            "Finish those sessions, then retry /update. No files were changed."
        )
    return "Nova is updating this installation. Try launching again when it finishes."


#: POSIX locks this process already owns: digest -> [stream, exclusive, depth].
#: The TUI updates in-process, inside the shared lock its own launch took, so a
#: nested request is served by the lock that is already open.
_HELD: dict[str, list] = {}


def _posix_lock(stream: object, *, exclusive: bool) -> None:
    import fcntl

    descriptors = getattr(stream, "fileno")
    flock = getattr(fcntl, "flock")
    mode = getattr(fcntl, "LOCK_EX" if exclusive else "LOCK_SH") | getattr(fcntl, "LOCK_NB")
    try:
        flock(descriptors(), mode)
    except OSError as error:
        raise UpdateBlockedError(_blocked_message(exclusive)) from error


def _windows_lock(stream: object, *, exclusive: bool) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel.LockFileEx.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(getattr(stream, "fileno")())
    overlap = Overlapped()
    flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
    if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
        error = ctypes.get_last_error()
        if error == 33:  # noqa: PLR2004 - Win32 error constant  # ERROR_LOCK_VIOLATION
            raise UpdateBlockedError(_blocked_message(exclusive))
        raise ctypes.WinError(error)


@contextmanager
def installation_lock(*, exclusive: bool, root: Path | None = None) -> Iterator[None]:
    """Exclude installers from each other and from runtimes of one installation.

    On POSIX the lock is re-entrant inside a process: an in-process update runs
    while its own launch holds the shared runtime lock, and it upgrades that
    lock instead of being refused by it. Windows installs in a fresh interpreter
    after the runtime exits, so there a second handle is still refused.
    """
    identity = os.path.normcase(str((root or Path(sys.prefix)).resolve()))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"nova-install-{digest}.lock"
    if sys.platform != "win32":
        import fcntl

        # Two installers writing one prefix corrupt it on POSIX just as on Windows.
        held = _HELD.get(digest)
        if held is None:
            stream = path.open("a+b")
            try:
                _posix_lock(stream, exclusive=exclusive)
            except BaseException:
                stream.close()
                raise
            held = [stream, exclusive, 1]
            _HELD[digest] = held
        else:
            held[2] += 1
            if exclusive and not held[1]:
                _posix_lock(held[0], exclusive=True)  # upgrade in place, same descriptor
                held[1] = True
        try:
            yield
        finally:
            held[2] -= 1
            if not held[2]:
                _HELD.pop(digest, None)
                with suppress(OSError):
                    fcntl.flock(held[0].fileno(), fcntl.LOCK_UN)
                held[0].close()
        return
    # Closing the handle releases the byte-range lock, even after os._exit or
    # a crash. Keep the empty file; deleting it would split the lock identity.
    with path.open("a+b") as stream:
        _windows_lock(stream, exclusive=exclusive)
        yield


def locked_processes(paths: list[Path]) -> list[tuple[int, str]]:
    """Ask Restart Manager which processes use these files; never stop them."""
    if not paths:
        return []
    import ctypes
    from ctypes import wintypes

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("process", UniqueProcess),
            ("name", wintypes.WCHAR * 256),
            ("service", wintypes.WCHAR * 64),
            ("kind", ctypes.c_int),
            ("status", wintypes.DWORD),
            ("session", wintypes.DWORD),
            ("restartable", wintypes.BOOL),
        ]

    api = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    api.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    api.RmRegisterResources.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT,
        ctypes.POINTER(UniqueProcess),
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
    ]
    api.RmGetList.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(ProcessInfo),
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.RmEndSession.argtypes = [wintypes.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(api, name).restype = wintypes.DWORD
    session = wintypes.DWORD()
    error = api.RmStartSession(ctypes.byref(session), 0, ctypes.create_unicode_buffer(33))
    if error:
        raise ctypes.WinError(error)
    try:
        names = (wintypes.LPCWSTR * len(paths))(*(str(path.resolve()) for path in paths))
        error = api.RmRegisterResources(session, len(paths), names, 0, None, 0, None)
        if error:
            raise ctypes.WinError(error)
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        entries: ctypes.Array[ProcessInfo] | None = None
        for _ in range(5):
            error = api.RmGetList(
                session, ctypes.byref(needed), ctypes.byref(count), entries, ctypes.byref(reasons)
            )
            if error == 0:
                if entries is None:
                    return []
                return [(entries[i].process.pid, entries[i].name) for i in range(count.value)]
            if error != 234:  # noqa: PLR2004 - Win32 error constant  # ERROR_MORE_DATA: process list changed; resize and retry.
                raise ctypes.WinError(error)
            count.value = needed.value
            entries = (ProcessInfo * count.value)()
        message = "Running processes changed during update checks. Retry /update."
        raise UpdateBlockedError(message)
    finally:
        api.RmEndSession(session)


#: Build output that never holds a module the installer must replace.
_PRUNED = {"__pycache__", ".git", "node_modules", "build", "dist", "__pypackages__"}


def _native_files(root: Path) -> list[Path]:
    """Native extension and library files under ``root``, skipping build output."""
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in _PRUNED]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in {".pyd", ".dll"}:
                path = Path(directory, filename)
                with suppress(OSError):
                    if path.is_file():
                        found.append(path)
    return found


def check_update_blockers(*, ignore_pids: set[int] | None = None, root: Path | None = None) -> None:
    """Reject mapped dependency files before uv/pip can remove any packages."""
    if sys.platform != "win32":
        return
    files = _native_files(root or Path(sys.prefix))
    ignored = {os.getpid(), *(ignore_pids or set())}
    blockers = [(pid, name) for pid, name in locked_processes(files) if pid not in ignored]
    if blockers:
        owners = ", ".join(f"{name or 'process'} (PID {pid})" for pid, name in blockers[:8])
        message = (
            f"Update paused: this installation is in use by {owners}. "
            "Finish those Nova sessions, then retry /update here. No files were changed."
        )
        raise UpdateBlockedError(message)
