"""A small async wrapper around a real pseudo-terminal for inline shell UI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import os
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_PTY_EXIT_DRAIN_SECONDS = 0.3


class PtyUnavailableError(RuntimeError):
    """The platform's pseudo-terminal backend is missing."""


class PtyShell:
    """Run one command with a TTY while Textual owns the outer terminal."""

    def __init__(self, process: Any, master: int | None = None) -> None:  # noqa: ANN401
        """Hold a platform process and its optional POSIX master fd."""
        self._process = process
        self._master = master
        self.pid: int = process.pid
        self._exit_seen_at: float | None = None

    @classmethod
    async def spawn(cls, command: str, cwd: Path, *, rows: int, columns: int) -> PtyShell:
        """Start a shell command in ConPTY on Windows or a POSIX PTY elsewhere."""
        if sys.platform == "win32":
            try:
                from winpty import PtyProcess
            except ImportError as exc:
                from novacode_cli.install_commands import install_command

                message = (
                    "Interactive shell needs pywinpty on Windows. Install it with: "
                    f"{install_command(['pywinpty'])}, then restart Nova."
                )
                raise PtyUnavailableError(message) from exc
            script = "& " + command if command.lstrip().startswith(('"', "'")) else command
            # PowerShell otherwise collapses a native child's nonzero status to 1.
            script += "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
            process = await asyncio.to_thread(
                PtyProcess.spawn,
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode("ascii"),
                ],
                cwd=str(cwd),
                env=os.environ.copy(),
                dimensions=(rows, columns),
            )
            process.fileobj.settimeout(0.1)
            return cls(process)

        import pty

        master, slave = pty.openpty()
        try:
            process = await asyncio.to_thread(  # noqa: S604 — explicit user shell command
                subprocess.Popen,
                command,
                shell=True,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=os.environ.copy(),
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(process, master)

    @property
    def returncode(self) -> int | None:
        """Return the exit code when the child has finished."""
        if self._master is not None:
            return self._process.poll()
        return None if self._process.isalive() else self._process.exitstatus

    async def read(self, size: int = 16384) -> str | None:  # noqa: PLR0911
        """Read output; return None while idle and an empty string at EOF."""
        if self._master is None:
            try:
                return await asyncio.to_thread(self._process.read, size)
            except EOFError:
                return ""
            except TimeoutError:
                if self.returncode is None:
                    self._exit_seen_at = None
                    return None
                if self._exit_seen_at is None:
                    self._exit_seen_at = time.monotonic()
                    return None
                return (
                    "" if time.monotonic() - self._exit_seen_at >= _PTY_EXIT_DRAIN_SECONDS else None
                )
        try:
            data = await asyncio.to_thread(os.read, self._master, size)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return ""
            raise
        return data.decode("utf-8", errors="replace")

    def write(self, text: str) -> None:
        """Send raw keys to the child; the caller owns focus routing."""
        if self._master is None:
            self._process.write(text)
        else:
            os.write(self._master, text.encode())

    async def wait(self) -> int:
        """Wait for the command and return its actual exit status."""
        code = await asyncio.to_thread(self._process.wait)
        return int(code or 0)

    def terminate(self) -> None:
        """Stop the child when a task is cancelled."""
        if self._master is None:
            self._process.terminate(force=True)
        else:
            self._process.terminate()

    def kill(self) -> None:
        """Force stop the child."""
        if self._master is None:
            self._process.terminate(force=True)
        else:
            self._process.kill()

    def close(self) -> None:
        """Release the master handle after the child is done."""
        if self._master is not None:
            os.close(self._master)
            self._master = None
        elif self._process.fileobj is not None:
            with contextlib.suppress(OSError):
                self._process.fileobj.shutdown(socket.SHUT_RDWR)
            self._process.fileobj.close()
            self._process._server.close()
