"""Windows installation ownership and read-only native file-lock inspection.

Keep this module stdlib-only: importing psutil here would itself map a package
extension that the clean updater must replace.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class UpdateBlockedError(ValueError):
    """Updating cannot safely begin; no package changes have been attempted."""


@contextmanager
def installation_lock(*, exclusive: bool, root: Path | None = None) -> Iterator[None]:
    """Allow concurrent runtimes, but exclude installers and new runtime starts."""
    if sys.platform != "win32":
        yield
        return
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

    identity = os.path.normcase(str((root or Path(sys.prefix)).resolve()))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"nova-install-{digest}.lock"
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
    # Closing the handle releases the byte-range lock, even after os._exit or
    # a crash. Keep the empty file; deleting it would split the lock identity.
    with path.open("a+b") as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        overlap = Overlapped()
        flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
        if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
            error = ctypes.get_last_error()
            if error == 33:  # noqa: PLR2004 - Win32 error constant  # ERROR_LOCK_VIOLATION
                message = (
                    "Other Nova sessions or an update are using this installation. "
                    "Finish those sessions, then retry /update. No files were changed."
                    if exclusive
                    else "Nova is updating this installation. Try launching again when it finishes."
                )
                raise UpdateBlockedError(message)
            raise ctypes.WinError(error)
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


def check_update_blockers(*, ignore_pids: set[int] | None = None, root: Path | None = None) -> None:
    """Reject mapped dependency files before uv/pip can remove any packages."""
    if sys.platform != "win32":
        return
    directory = root or Path(sys.prefix)
    files = [
        p for p in directory.rglob("*") if p.suffix.lower() in {".pyd", ".dll"} and p.is_file()
    ]
    ignored = {os.getpid(), *(ignore_pids or set())}
    blockers = [(pid, name) for pid, name in locked_processes(files) if pid not in ignored]
    if blockers:
        owners = ", ".join(f"{name or 'process'} (PID {pid})" for pid, name in blockers[:8])
        message = (
            f"Update paused: this installation is in use by {owners}. "
            "Finish those Nova sessions, then retry /update here. No files were changed."
        )
        raise UpdateBlockedError(message)
