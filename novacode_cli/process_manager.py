"""Process lifecycle management for managed subprocesses.

This module provides functionality to track, manage, and clean up
subprocesses started by the CLI, including test runners and dev servers.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class ProcessStatus(Enum):
    """Status of a managed process."""

    STARTING = "starting"
    RUNNING = "running"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    FAILED = "failed"


# Maximum number of captured output lines retained per managed process. The
# buffer is only read via ``ProcessInfo.output`` (used for short-lived test/dev
# runs); an unbounded list leaks memory for long-running servers.
_MAX_OUTPUT_LINES = 5000


@dataclass
class ProcessInfo:
    """Information about a managed process.

    Attributes:
        pid: Process ID
        name: Human-readable name (e.g., "pytest", "npm run dev")
        command: Full command that was executed
        port: Port the process is listening on (if applicable)
        status: Current status of the process
        started_at: Timestamp when process was started
        working_dir: Working directory for the process
        health_check_url: URL to check for health (for servers)
    """

    pid: int
    name: str
    command: str
    port: int | None = None
    status: ProcessStatus = ProcessStatus.STARTING
    started_at: datetime = field(default_factory=datetime.now)
    working_dir: str = "."
    health_check_url: str | None = None
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _output_lines: list[str] = field(default_factory=list, repr=False)

    @property
    def is_alive(self) -> bool:
        """Check if the process is still running."""
        if self._process is None:
            return False
        return self._process.returncode is None

    @property
    def exit_code(self) -> int | None:
        """Get the exit code if the process has terminated."""
        if self._process is None:
            return None
        return self._process.returncode

    @property
    def output(self) -> str:
        """Get the captured output."""
        return "\n".join(self._output_lines)

    def _append_output(self, line: str) -> None:
        """Append a captured output line, bounding the buffer.

        The buffer is only read via ``output`` (used for short-lived test/dev
        runs); an unbounded list leaks memory for long-running servers.
        """
        self._output_lines.append(line)
        if len(self._output_lines) > _MAX_OUTPUT_LINES:
            del self._output_lines[: len(self._output_lines) - _MAX_OUTPUT_LINES]


class ProcessManager:
    """Singleton manager for tracked subprocess lifecycle.

    This class manages all subprocesses started by the CLI, ensuring:
    - Processes are tracked by PID and name
    - Health monitoring for server processes
    - Graceful cleanup on session exit
    - Output streaming to UI

    Usage:
        manager = ProcessManager.get_instance()
        info = await manager.start_process("npm run dev", name="dev-server", port=3000)
        await manager.stop_process(info.pid)
    """

    _instance: ProcessManager | None = None

    def __init__(self) -> None:
        """Initialize ProcessManager. Use get_instance() instead."""
        self._processes: dict[int, ProcessInfo] = {}
        self._name_to_pid: dict[str, int] = {}
        self._cleanup_registered = False
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> ProcessManager:
        """Get singleton instance of ProcessManager."""
        if cls._instance is None:
            cls._instance = ProcessManager()
            cls._instance._register_cleanup()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance. Mainly for testing."""
        if cls._instance is not None:
            # Stop all processes synchronously
            cls._instance._sync_cleanup_all()
        cls._instance = None

    def _register_cleanup(self) -> None:
        """Register cleanup handlers for process termination."""
        if self._cleanup_registered:
            return

        atexit.register(self._sync_cleanup_all)

        # Handle SIGINT/SIGTERM gracefully (Unix only)
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    # Store original handler to chain if needed
                    original = signal.getsignal(sig)
                    if original not in (signal.SIG_IGN, signal.SIG_DFL, None):
                        # Don't override existing handlers
                        continue
                    signal.signal(sig, self._signal_handler)
                except (ValueError, OSError):
                    pass  # May fail in non-main thread

        self._cleanup_registered = True

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle termination signals by cleaning up processes."""
        self._sync_cleanup_all()
        # Re-raise the signal to allow normal termination
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _sync_cleanup_all(self) -> None:
        """Synchronous cleanup for atexit handler."""
        for pid, info in list(self._processes.items()):
            if info._process and info.is_alive:
                try:
                    # Close stdin (StreamWriter) to prevent ResourceWarning on Windows
                    # Note: stdout/stderr are StreamReader and don't have close()
                    if info._process.stdin:
                        info._process.stdin.close()

                    info._process.terminate()
                    # Give it a moment to terminate - use kill() on Windows
                    # since wait() can cause issues with ProactorEventLoop
                    if sys.platform == "win32":
                        try:
                            info._process.kill()
                        except Exception:
                            pass
                    else:
                        try:
                            info._process.wait()
                        except Exception:
                            info._process.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
        self._processes.clear()
        self._name_to_pid.clear()

    async def start_process(
        self,
        command: str,
        *,
        name: str,
        port: int | None = None,
        working_dir: str | Path = ".",
        env: dict[str, str] | None = None,
        health_check_url: str | None = None,
        output_callback: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> ProcessInfo:
        """Start a managed subprocess.

        Args:
            command: Shell command to execute
            name: Human-readable name for the process
            port: Port the process will listen on (for servers)
            working_dir: Working directory for the process
            env: Environment variables (merged with current env)
            health_check_url: URL to poll for health checks
            output_callback: Callback for streaming output lines
            timeout: Timeout for process startup (seconds)

        Returns:
            ProcessInfo for the started process

        Raises:
            RuntimeError: If process fails to start
        """
        async with self._lock:
            # Prepare environment
            process_env = os.environ.copy()
            if env:
                process_env.update(env)

            # Start the subprocess
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(working_dir),
                    env=process_env,
                )
            except Exception as e:
                raise RuntimeError(f"Failed to start process: {e}") from e

            # Create ProcessInfo
            info = ProcessInfo(
                pid=process.pid,
                name=name,
                command=command,
                port=port,
                status=ProcessStatus.RUNNING,
                started_at=datetime.now(),
                working_dir=str(working_dir),
                health_check_url=health_check_url,
                _process=process,
            )

            # Track the process
            self._processes[process.pid] = info
            self._name_to_pid[name] = process.pid

            # Start output streaming task (non-blocking)
            if output_callback:
                from novacode_cli.utils.tasks import spawn

                spawn(self._stream_output(process, info, output_callback))

            return info

    def register_process(self, info: ProcessInfo) -> None:
        """Register an externally-created process for tracking and cleanup.

        For callers that spawn their own subprocess (e.g. ShellMiddleware's
        background servers) but want it tracked and terminated on exit. Best-effort
        and lock-free: the async lock guards start/stop coordination; this is just
        bookkeeping into the same registries the public accessors read.
        """
        self._processes[info.pid] = info
        if info.name:
            self._name_to_pid[info.name] = info.pid

    def _remove_process(self, pid: int) -> None:
        """Drop a process from the registries once it has terminated.

        Completed processes are never read back (all consumers use
        ``list_processes(alive_only=True)``), so keeping them in ``_processes``
        leaks memory across a long session. Best-effort and lock-free, matching
        ``register_process``.
        """
        info = self._processes.pop(pid, None)
        if info is not None and info.name and self._name_to_pid.get(info.name) == pid:
            # Only clear the name mapping if it still points at this pid.
            self._name_to_pid.pop(info.name, None)

    async def _stream_output(
        self,
        process: asyncio.subprocess.Process,
        info: ProcessInfo,
        callback: Callable[[str], None],
    ) -> None:
        """Stream process output to callback in real-time."""
        try:
            while True:
                if process.stdout is None:
                    break
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n\r")
                info._append_output(decoded)
                try:
                    callback(decoded)
                except Exception:
                    pass  # Don't let callback errors kill streaming
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Process may have terminated
        finally:
            # The process has exited (EOF) or streaming was cancelled; drop it
            # from the registries so completed processes don't accumulate.
            self._remove_process(info.pid)

    async def stop_process(
        self,
        pid: int,
        *,
        timeout: float = 10.0,
        force: bool = False,
    ) -> bool:
        """Stop a managed process.

        Args:
            pid: Process ID to stop
            timeout: Graceful shutdown timeout before SIGKILL
            force: If True, use SIGKILL immediately

        Returns:
            True if process was stopped, False if not found
        """
        async with self._lock:
            info = self._processes.get(pid)
            if info is None:
                return False

            if info._process is None or not info.is_alive:
                # Already stopped
                info.status = ProcessStatus.STOPPED
                self._remove_process(pid)
                return True

            try:
                if force:
                    info._process.kill()
                else:
                    info._process.terminate()

                try:
                    await asyncio.wait_for(info._process.wait(), timeout=timeout)
                except asyncio.CancelledError:
                    info._process.kill()
                    await info._process.wait()
                    raise
                except TimeoutError:
                    # Force kill if graceful termination timed out
                    info._process.kill()
                    await info._process.wait()

                info.status = ProcessStatus.STOPPED

            except ProcessLookupError:
                # Process already gone
                info.status = ProcessStatus.STOPPED
            except Exception:
                info.status = ProcessStatus.FAILED
                return False
            finally:
                # Close stdin (StreamWriter) after process terminates to prevent
                # ResourceWarning on Windows. stdout/stderr are StreamReader and
                # don't have close().
                try:
                    if info._process.stdin:
                        info._process.stdin.close()
                except Exception:
                    pass
                # Drop the terminated process from the registries so completed
                # processes don't accumulate.
                self._remove_process(pid)

            return True

    async def stop_by_name(self, name: str, **kwargs: Any) -> bool:
        """Stop a process by its name.

        Args:
            name: Name of the process to stop
            **kwargs: Additional arguments passed to stop_process

        Returns:
            True if process was stopped, False if not found
        """
        pid = self._name_to_pid.get(name)
        if pid is None:
            return False
        return await self.stop_process(pid, **kwargs)

    async def stop_all(self, timeout: float = 10.0) -> int:
        """Stop all managed processes.

        Args:
            timeout: Timeout per process for graceful shutdown

        Returns:
            Number of processes that were stopped
        """
        stopped_count = 0
        for pid in list(self._processes.keys()):
            if await self.stop_process(pid, timeout=timeout):
                stopped_count += 1
        return stopped_count

    def get_process(self, pid: int) -> ProcessInfo | None:
        """Get process info by PID.

        Args:
            pid: Process ID

        Returns:
            ProcessInfo if found, None otherwise
        """
        return self._processes.get(pid)

    def get_by_name(self, name: str) -> ProcessInfo | None:
        """Get process info by name.

        Args:
            name: Process name

        Returns:
            ProcessInfo if found, None otherwise
        """
        pid = self._name_to_pid.get(name)
        return self._processes.get(pid) if pid else None

    def list_processes(self, *, alive_only: bool = True) -> list[ProcessInfo]:
        """List all managed processes.

        Args:
            alive_only: If True, only return running processes

        Returns:
            List of ProcessInfo objects
        """
        processes = list(self._processes.values())
        if alive_only:
            processes = [p for p in processes if p.is_alive]
        return processes

    async def check_health(self, pid: int) -> ProcessStatus:
        """Check health of a server process.

        Uses HTTP health check if health_check_url is set,
        otherwise checks if process is alive.

        Args:
            pid: Process ID

        Returns:
            Current ProcessStatus
        """
        info = self._processes.get(pid)
        if info is None:
            return ProcessStatus.STOPPED

        if not info.is_alive:
            info.status = ProcessStatus.STOPPED
            return ProcessStatus.STOPPED

        # If there's a health check URL, try HTTP
        if info.health_check_url:
            try:
                import aiohttp

                async with (
                    aiohttp.ClientSession() as session,
                    session.get(
                        info.health_check_url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp,
                ):
                    if resp.status < 400:
                        info.status = ProcessStatus.HEALTHY
                    else:
                        info.status = ProcessStatus.UNHEALTHY
            except Exception:
                info.status = ProcessStatus.UNHEALTHY
        else:
            # No health check URL, just check if alive
            info.status = ProcessStatus.RUNNING

        return info.status


async def stream_subprocess_output(
    command: str,
    working_dir: str,
    callback: Callable[[str], None],
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Execute subprocess with real-time output streaming.

    This is a standalone utility function for running commands to completion
    with real-time output (e.g., for test execution).

    Args:
        command: Shell command to execute
        working_dir: Working directory
        callback: Callback for each output line
        timeout: Maximum execution time in seconds
        env: Optional environment variables

    Returns:
        Tuple of (exit_code, full_output)

    Raises:
        asyncio.TimeoutError: If process exceeds timeout
    """
    # Prepare environment
    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=working_dir,
        env=process_env,
    )

    output_lines: list[str] = []

    async def read_stream() -> None:
        while True:
            if process.stdout is None:
                break
            line = await process.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n\r")
            output_lines.append(decoded)
            try:
                callback(decoded)
            except Exception:
                pass

    try:
        await asyncio.wait_for(read_stream(), timeout=timeout)
        await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    finally:
        # Close stdin (StreamWriter) to prevent ResourceWarning on Windows
        # Note: stdout/stderr are StreamReader and don't have close()
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass

    return process.returncode or 0, "\n".join(output_lines)
