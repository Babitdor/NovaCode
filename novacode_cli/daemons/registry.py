"""Detached-daemon registry — a JSON file-backed pid store that survives restarts.

A *daemon* is a long-lived background process launched by the ``daemon`` tool that
outlives the CLI session (unlike an in-session :class:`~novacode_cli.shell.jobs.BackgroundJob`,
which dies with the CLI). This module tracks those daemons by name in a small JSON
file under ``~/.nova/daemons/registry.json`` so the agent can list, check, and stop
them across CLI restarts.

Because the registry outlives the process that wrote it, three things that would
be safe in a single session are not safe here, and each is handled explicitly:

**A pid is not an identity.** Pids are recycled, so a stale entry can name an
unrelated live process. Every entry records the process's real creation time and
:func:`is_ours` checks it before anything acts on the pid — without that,
stopping a long-dead daemon could force-kill a stranger's process tree.

**Another session may be writing too.** The registry is explicitly shared, so a
thread lock is not enough: reads and writes take a cross-process file lock, and
the temp file is per-process, or concurrent sessions lose each other's entries
(measured: 43 of 80) and can promote a half-written file over the real one.

**A name becomes a path.** Names are sanitized before they reach the filesystem;
``../..`` in a name would otherwise write a log outside the daemons directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Any

#: Directory holding daemon logs + the registry.
DAEMONS_DIR = Path.home() / ".nova" / "daemons"
#: Registry file path.
REGISTRY_PATH = DAEMONS_DIR / "registry.json"

_lock = threading.RLock()

#: Maximum wait for an OS lock held by a live or stalled writer.
_LOCK_TIMEOUT = 5.0

#: Tolerance when matching a recorded creation time against the live process.
#: Windows FILETIME and the epoch conversion round; the value only needs to be
#: far tighter than the gap between a pid dying and being reissued.
_CREATE_TIME_TOLERANCE = 2.0


def _lock_path() -> Path:
    return REGISTRY_PATH.with_name(REGISTRY_PATH.name + ".lock")


@contextlib.contextmanager
def _file_lock() -> Iterator[None]:
    """Use an OS lock that a dead process releases without unlink races."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        while not locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Daemon registry lock timed out") from exc
                time.sleep(0.01)
        yield
    finally:
        if locked:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def safe_name(name: str) -> str:
    """Reduce *name* to something that cannot escape the daemons directory.

    ``name`` is chosen by the agent and is interpolated into a log path, so
    ``../../x`` would otherwise write outside ``~/.nova/daemons``. Mirrors the
    sanitizer in :mod:`novacode_cli.integrations.sandbox_registry`.
    """
    cleaned = "".join(c for c in str(name) if c.isalnum() or c in "-_.")[:120]
    # A name of dots alone still resolves to a parent directory.
    if not cleaned or set(cleaned) <= {"."}:
        return "unnamed"
    return cleaned


@dataclass
class DaemonInfo:
    """A registered daemon's metadata."""

    name: str
    pid: int
    command: str
    log_path: str
    started_at: float
    cwd: str = "."
    #: The OS-reported process creation time, used to tell this daemon apart
    #: from whatever later inherits its pid. 0.0 when the platform could not
    #: report one (then only liveness is known — see :func:`is_ours`).
    create_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-able dict."""
        return {
            "name": self.name,
            "pid": self.pid,
            "command": self.command,
            "log_path": self.log_path,
            "started_at": self.started_at,
            "cwd": self.cwd,
            "create_time": self.create_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DaemonInfo:
        """Deserialize from a dict (as written by :meth:`to_dict`)."""
        return cls(
            name=str(data.get("name", "")),
            pid=int(data.get("pid", 0)),
            command=str(data.get("command", "")),
            log_path=str(data.get("log_path", "")),
            started_at=float(data.get("started_at", 0.0)),
            cwd=str(data.get("cwd", ".")),
            create_time=float(data.get("create_time", 0.0) or 0.0),
        )


def _read() -> dict[str, dict[str, Any]]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        return {}
    return {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    # Per-process temp name: a shared one lets two sessions interleave into a
    # single half-written file and then promote it over the real registry.
    tmp = REGISTRY_PATH.with_name(f"{REGISTRY_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(REGISTRY_PATH)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()


def register(info: DaemonInfo) -> None:
    """Add or update a daemon in the registry."""
    with _lock, _file_lock():
        data = _read()
        data[info.name] = info.to_dict()
        _write(data)


def unregister(name: str) -> bool:
    """Remove a daemon by name. Returns True if it was present."""
    with _lock, _file_lock():
        data = _read()
        if name not in data:
            return False
        del data[name]
        _write(data)
        return True


def get(name: str) -> DaemonInfo | None:
    """Look up a daemon by name, or None."""
    with _lock, _file_lock():
        data = _read()
        raw = data.get(name)
        if raw is None:
            return None
        return DaemonInfo.from_dict(raw)


def list_daemons() -> list[DaemonInfo]:
    """Return all registered daemons, sorted by name."""
    with _lock, _file_lock():
        data = _read()
        return sorted(
            (DaemonInfo.from_dict(raw) for raw in data.values()),
            key=lambda d: d.name,
        )


def process_created_at(pid: int) -> float | None:
    """The OS-reported creation time of *pid* as a unix timestamp, or None.

    None means "this platform could not tell us", not "the process is gone".
    Windows and Linux are covered; elsewhere identity cannot be confirmed and
    callers fall back to liveness alone.
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_t, kernel_t, user_t = (
                    wintypes.FILETIME(),
                    wintypes.FILETIME(),
                    wintypes.FILETIME(),
                )
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_t),
                    ctypes.byref(kernel_t),
                    ctypes.byref(user_t),
                )
                if not ok:
                    return None
                ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                # FILETIME: 100 ns units since 1601-01-01.
                return ticks / 10_000_000.0 - 11_644_473_600.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 — probing must never raise
            return None
    try:
        # Linux: field 22 of /proc/<pid>/stat is start time in clock ticks
        # since boot; /proc/stat's btime is boot time in epoch seconds.
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat[stat.rindex(")") + 2 :].split()
        start_ticks = float(fields[19])
        hz = os.sysconf("SC_CLK_TCK")
        boot = 0.0
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
                break
        if not boot or not hz:
            return None
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return boot + start_ticks / hz


def is_alive(pid: int) -> bool:
    """True when a process with *pid* exists.

    Conservative on purpose: only a *definite* "no such process" returns False.
    An indeterminate probe (a live process we lack rights to open) must not read
    as dead, or the caller starts a duplicate daemon and orphans the first.
    """
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by another user
        except OSError:
            return True  # indeterminate — assume alive
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — can't probe ⇒ assume alive
        return True


def is_ours(info: DaemonInfo) -> bool:
    """True when *info*'s pid is still the daemon we registered.

    A live pid is not enough. Pids are reused, and this registry survives
    reboots, so an entry can name a process that merely inherited the number —
    acting on that would stop (or force-kill the tree of) an unrelated process.
    """
    if not is_alive(info.pid):
        return False
    if not info.create_time:
        # Recorded before creation times were captured, or a platform that
        # cannot report one: liveness is all we know.
        return True
    actual = process_created_at(info.pid)
    if actual is None:
        return True
    return abs(actual - info.create_time) <= _CREATE_TIME_TOLERANCE


def log_path_for(name: str) -> Path:
    """Return the log file path for a daemon (without creating it)."""
    return DAEMONS_DIR / f"{safe_name(name)}.log"
