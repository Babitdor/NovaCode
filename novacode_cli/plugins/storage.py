"""Atomic plugin metadata writes under an OS-released cross-process lock."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_lock = threading.RLock()


@contextlib.contextmanager
def locked(directory: Path) -> Iterator[None]:
    """Serialize mutations; crashed owners release their OS lock automatically."""
    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / ".registry.lock", os.O_CREAT | os.O_RDWR)
        acquired = False
        try:
            deadline = time.monotonic() + 10
            while not acquired:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import importlib

                        fcntl = importlib.import_module("fcntl")

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        msg = "Plugin registry is busy"
                        raise TimeoutError(msg) from exc
                    time.sleep(0.02)
            yield
        finally:
            if acquired:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def write_json(path: Path, data: dict) -> None:
    """Publish complete JSON with a unique temporary file on the same volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".plugin-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
