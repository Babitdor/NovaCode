"""Launch a local ``langgraph dev`` server so async subagents work without Docker.

Nova's dynamic (remote) subagents talk Agent Protocol to a LangGraph server
(`agents/default_subagents/async_subagents.py`). Until now that server was the
``novacode`` Docker container, so the six agents were silently withheld whenever
the container was not running. This module starts the same six graphs locally,
on demand, and points the existing client at it — the client's contract is
unchanged, only its reachability probe starts succeeding.

Adapted from the ``deepagents_code`` CLI's launcher (``Check/launch/server.py``):
the port selection, health/readiness polling, process-group teardown and the
lifecycle state machine are its design. Its environment handling is not — that
was coupled to that CLI's profile system and LangSmith carriers, and Nova owns
its own policy here.

Deliberately soft-failing: a server that will not come up must leave the session
exactly as it was (in-process subagents), never block it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import socket
import subprocess  # this module exists to launch one
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Self, cast
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
#: 0 asks the OS for a free port, so a locally launched server never squats the
#: well-known 2024 that the Docker container (and a user's own server) uses.
EPHEMERAL_PORT = 0
HEALTH_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 5.0
KILL_TIMEOUT = 2.0
HEALTH_POLL_INTERVAL = 0.15
LOG_TAIL_CHARS = 3000
#: The only success status these probes accept.
HTTP_OK = 200

#: Present only on Windows, so the literals are used (mirrors the blueprint).
_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
#: Load-bearing value: ``Popen.send_signal`` dispatches on it exactly; 0 would
#: mean CTRL_C_EVENT, which the new process group deliberately ignores.
_WINDOWS_CTRL_BREAK_EVENT = 1
#: Flag naming the tree kill used when a graceful stop leaves the server alive.
_TASKKILL_TIMEOUT = 10

#: Inherited environment variables that must never reach the server child. These
#: are the ones that let a parent process hijack the child interpreter or its
#: loader; the rest of the environment is forwarded so the Ollama and model
#: settings the graphs read (`OLLAMA_HOST`, `DOC_AGENT_MODEL`, ...) arrive intact.
_ENV_DENYLIST = frozenset(
    {
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GIT_ASKPASS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "SSH_ASKPASS",
    }
)

#: The one process this module manages per session, in a mutable cell so no
#: function needs a ``global`` statement to replace it.
_state: dict[str, AgentServerProcess | None] = {"process": None}
_server_lock = threading.Lock()


def server_extra_available() -> bool:
    """Whether the ``agents-server`` extra (``langgraph-cli``) is installed.

    A missing extra is the normal state for most installs, so callers should
    treat ``False`` as "nothing to do" rather than an error.
    """
    import importlib.util

    return importlib.util.find_spec("langgraph_cli") is not None


def get_server_url(host: str = DEFAULT_HOST, port: int = EPHEMERAL_PORT) -> str:
    """``http://host:port`` for a server bound at *host* and *port*."""
    return f"http://{host}:{port}"


def port_in_use(host: str, port: int) -> bool:
    """Whether something is already listening on *host*:*port*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((host, port)) == 0


def find_free_port(host: str = DEFAULT_HOST) -> int:
    """A port the OS says is free right now.

    There is an unavoidable race between asking and binding; the caller retries
    once on a fresh port if the server fails to bind.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def build_command(*, host: str, port: int, config_path: Path) -> list[str]:
    """The argv for the in-memory dev server, run by this interpreter.

    ``--no-browser`` matters in a terminal agent (it must not open a tab) and
    ``--no-reload`` matters because a reloader would re-exec the server and
    orphan the process we are tracking.
    """
    return [
        sys.executable,
        "-m",
        "langgraph_cli",
        "dev",
        "--host",
        host,
        "--port",
        str(port),
        "--no-browser",
        "--no-reload",
        "--config",
        str(config_path),
    ]


def build_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The child environment: Nova's own policy, not the parent's verbatim.

    Deliberately *not* stripping the API keys a model may need (the graphs
    default to Ollama, which authenticates with `OLLAMA_API_KEY` when the model
    is a hosted one) — only the loader-hijacking variables, plus the two values
    Nova sets itself.
    """
    env = {key: value for key, value in os.environ.items() if key not in _ENV_DENYLIST}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # A local server has no auth; without this the CLI's default auth would make
    # the client's unauthenticated requests fail.
    env["LANGGRAPH_AUTH_TYPE"] = "noop"
    if overrides:
        env.update({key: str(value) for key, value in overrides.items()})
    return env


async def wait_healthy(
    url: str,
    *,
    process: subprocess.Popen[bytes] | None = None,
    read_log: Callable[[], str] | None = None,
    timeout: float = HEALTH_TIMEOUT,  # noqa: ASYNC109 — budget, not asyncio.timeout
) -> None:
    """Wait for ``GET {url}/ok`` to answer 200, or raise.

    A server that exits early is reported at once with its log tail, rather than
    after the full timeout: that is the difference between a legible failure and
    a minute of silence.
    """
    import httpx

    deadline = time.monotonic() + timeout
    health_url = f"{url}/ok"
    last_status: int | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            msg = (
                f"agent server exited during startup (code {process.returncode})"
                f"{_log_tail(read_log)}"
            )
            raise RuntimeError(msg)
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(health_url)
        except (httpx.TransportError, httpx.TimeoutException, OSError):
            await asyncio.sleep(HEALTH_POLL_INTERVAL)
            continue
        if response.status_code == HTTP_OK:
            return
        last_status = response.status_code
        await asyncio.sleep(HEALTH_POLL_INTERVAL)
    detail = f" (last status {last_status})" if last_status is not None else ""
    msg = f"agent server did not become healthy within {timeout:g}s{detail}{_log_tail(read_log)}"
    raise RuntimeError(msg)


async def wait_graph_ready(
    url: str,
    graph: str,
    *,
    process: subprocess.Popen[bytes] | None = None,
    read_log: Callable[[], str] | None = None,
    timeout: float = HEALTH_TIMEOUT,  # noqa: ASYNC109 — budget, not asyncio.timeout
) -> None:
    """Wait until the server has compiled *graph*.

    ``/ok`` only proves uvicorn is up; the graph endpoint is what proves the
    graphs in ``langgraph.json`` imported. A local server that is healthy but
    whose graph failed to import would otherwise fail on the first delegation.
    """
    import httpx

    deadline = time.monotonic() + timeout
    graph_url = f"{url}/assistants/{quote(graph, safe='')}/graph"
    last_status: int | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            msg = f"agent server exited before graph '{graph}' was ready{_log_tail(read_log)}"
            raise RuntimeError(msg)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(graph_url)
        except (httpx.TransportError, httpx.TimeoutException, OSError):
            await asyncio.sleep(HEALTH_POLL_INTERVAL)
            continue
        if response.status_code == HTTP_OK:
            return
        # Retry rather than fail: a 404 here means the server has not compiled
        # the graph yet, which is exactly what this function exists to wait for.
        last_status = response.status_code
        await asyncio.sleep(HEALTH_POLL_INTERVAL)
    detail = f" (last status {last_status})" if last_status is not None else ""
    msg = f"graph '{graph}' did not become ready within {timeout:g}s{detail}{_log_tail(read_log)}"
    raise RuntimeError(msg)


def _log_tail(read_log: Callable[[], str] | None) -> str:
    """The last of the server log, for an error message (empty if unavailable)."""
    if read_log is None:
        return ""
    with contextlib.suppress(Exception):
        tail = read_log()[-LOG_TAIL_CHARS:].strip()
        if tail:
            return f"\n--- server log ---\n{tail}"
    return ""


def _process_group(pid: int) -> int | None:
    """The POSIX process group to signal, or ``None`` when unsafe on Windows."""
    if sys.platform == "win32":
        return None
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    except OSError:
        logger.warning("could not read the process group of pid %s", pid)
        return None
    # Never signal a group that is not the server's own, and never our own
    # group: a stray killpg there would take the user's session down with it.
    if pgid != pid or pgid == os.getpgid(0):
        return None
    return pgid


def _wait_group_exit(process: subprocess.Popen[bytes], pgid: int, timeout: float) -> bool:
    """Wait for a whole POSIX process group to disappear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.poll()  # reap the leader, or the group probe stays alive
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            with contextlib.suppress(Exception):
                process.wait(timeout=KILL_TIMEOUT)
            return True
        except PermissionError:
            pass  # the group still exists
        time.sleep(0.05)
    return False


def _signal_windows(process: subprocess.Popen[bytes]) -> None:
    """Ask a Windows console-group leader for a graceful shutdown (Ctrl+Break)."""
    try:
        process.send_signal(_WINDOWS_CTRL_BREAK_EVENT)
    except ProcessLookupError:
        raise
    except OSError:
        logger.warning("Ctrl+Break failed; terminating the server process instead")
        with contextlib.suppress(OSError):
            process.terminate()


def _taskkill_tree(pid: int) -> None:
    """Windows escalation that reaches descendants, not just the root.

    ``process.kill()`` kills the root handle only, and ``langgraph dev`` runs its
    server in the same console group, so an escalated kill without this can leave
    a process holding the port.
    """
    taskkill = shutil.which("taskkill")
    if taskkill is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603
            [taskkill, "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT,
            check=False,
        )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop *process* gracefully, then forcefully, covering its children."""
    pgid = _process_group(process.pid)
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
            if _wait_group_exit(process, pgid, SHUTDOWN_TIMEOUT):
                return
        elif sys.platform == "win32":
            _signal_windows(process)
            process.wait(timeout=SHUTDOWN_TIMEOUT)
            return
        else:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=SHUTDOWN_TIMEOUT)
            return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    except OSError:
        logger.exception("failed to stop the agent server gracefully; it may be orphaned")

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
            _wait_group_exit(process, pgid, KILL_TIMEOUT)
        else:
            process.kill()
            process.wait(timeout=KILL_TIMEOUT)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if sys.platform == "win32":
        _taskkill_tree(process.pid)


class AgentServerProcess:
    """A locally launched ``langgraph dev`` server.

    Keyword-only by design: a caller that gets host/port/config wrong should fail
    at the call site rather than silently starting the wrong thing.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        cwd: Path,
        graph_names: list[str],
        host: str = DEFAULT_HOST,
        port: int = EPHEMERAL_PORT,
        timeout: float = HEALTH_TIMEOUT,
    ) -> None:
        """Remember what to launch; nothing is started until :meth:`start`."""
        self.host = host
        self.port = port
        self.config_path = Path(config_path)
        self.cwd = Path(cwd)
        self.graph_names = list(graph_names)
        self.timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._log_file: IO[str] | None = None
        self._log_path: Path | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._stopped = False

    # ── introspection ───────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        """The base URL the client should use (reflects the port actually bound)."""
        return get_server_url(self.host, self.port)

    @property
    def running(self) -> bool:
        """Whether the server process is alive."""
        with self._state_lock:
            return self._process is not None and self._process.poll() is None

    @property
    def log_path(self) -> Path | None:
        """Where the server's merged stdout/stderr went, if it has started."""
        return self._log_path

    def read_log(self) -> str:
        """The whole server log so far (``""`` when there is none yet)."""
        with self._state_lock:
            handle, path = self._log_file, self._log_path
        if handle is None or path is None:
            return ""
        with contextlib.suppress(Exception):
            handle.flush()
            return path.read_text(encoding="utf-8", errors="replace")
        return ""

    def log_tail(self, lines: int = 40) -> str:
        """The last *lines* lines of the server log, for `/agents logs`."""
        return "\n".join(self.read_log().splitlines()[-lines:])

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the server and wait for it and its first graph to be ready."""
        async with self._lifecycle_lock:
            if self.running:
                return
            self._stopped = False
            process = self._spawn()
            try:
                await wait_healthy(
                    self.url,
                    process=process,
                    read_log=self.read_log,
                    timeout=self.timeout,
                )
                # Only the first graph: one server hosts them all, so the first
                # one compiling proves the workspace is importable.
                for graph in self.graph_names[:1]:
                    await wait_graph_ready(
                        self.url,
                        graph,
                        process=process,
                        read_log=self.read_log,
                        timeout=self.timeout,
                    )
            except BaseException:
                # Deliberately BaseException: a cancelled start must not leave a
                # server running that nothing will ever stop.
                await asyncio.to_thread(self.stop)
                raise

    def _spawn(self) -> subprocess.Popen[bytes]:
        """Reserve a port, then spawn the server on it."""
        if self.port == EPHEMERAL_PORT:
            self.port = find_free_port(self.host)
            logger.debug("agent server will use ephemeral port %s", self.port)
        elif port_in_use(self.host, self.port):
            requested, self.port = self.port, find_free_port(self.host)
            logger.info("port %s is in use; using %s instead", requested, self.port)
        command = build_command(host=self.host, port=self.port, config_path=self.config_path)
        log_handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 — lives as long as the server
            prefix="nova_agent_server_log_",
            suffix=".txt",
            delete=False,
            mode="w",
            encoding="utf-8",
        )
        logger.info("starting agent server: %s", " ".join(command))
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(self.cwd),
            env=build_env(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=(sys.platform != "win32"),
            creationflags=(_WINDOWS_CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
        )
        with self._state_lock:
            self._process = process
            self._log_file = cast("IO[str]", log_handle)
            self._log_path = Path(log_handle.name)
        return process

    def stop(self) -> None:
        """Stop the server. Idempotent, and safe to call from any thread."""
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
            process, handle, path = self._process, self._log_file, self._log_path
            self._process = None
        if process is not None and process.poll() is None:
            _terminate(process)
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()
        if path is not None:
            logger.debug("agent server log: %s", path)

    async def restart(self) -> None:
        """Stop and start again, on a freshly chosen port."""
        await asyncio.to_thread(self.stop)
        self.port = EPHEMERAL_PORT
        await self.start()

    async def __aenter__(self) -> Self:
        """Start the server and return it, for ``async with``."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Stop the server on the way out."""
        self.stop()


# ── the session-level policy ────────────────────────────────────────────────


def _config_path() -> Path:
    """Nova's ``langgraph.json``, which already registers the six graphs."""
    return Path(__file__).resolve().parents[2] / "langgraph.json"


def _graph_names(config_path: Path) -> list[str]:
    """Graph names from ``langgraph.json``, in file order."""
    import json

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config just means no graphs
        return []
    graphs = data.get("graphs") if isinstance(data, dict) else None
    return list(graphs) if isinstance(graphs, dict) else []


async def ensure_agent_server(
    *,
    enabled: bool = True,
    port: int | None = None,
    timeout: float = HEALTH_TIMEOUT,  # noqa: ASYNC109 — budget, not asyncio.timeout
) -> AgentServerProcess | None:
    """Start a local agent server if one is needed and possible.

    Returns the process it started, or ``None`` when it did nothing — which is
    the common case: another server is already answering, the optional extra is
    not installed, or the launch failed. Callers treat ``None`` as "carry on with
    the in-process subagents" and never as an error.

    Blocking by design: the async-subagent specs are read while the agent is
    built, so the server has to be up before that happens.
    """
    if not enabled:
        return None

    from novacode_cli.agents.default_subagents.async_subagents import (
        async_agents_available,
    )

    if async_agents_available(refresh=True):
        logger.debug("an agent server is already reachable; not launching one")
        return None

    if not server_extra_available():
        logger.info(
            "async agents need a LangGraph server: run the Docker container, or "
            "install the optional extra (uv sync --extra agents-server) so Nova "
            "can launch one locally"
        )
        return None

    config_path = _config_path()
    graphs = _graph_names(config_path)
    if not config_path.is_file() or not graphs:
        logger.warning("cannot launch an agent server: %s has no graphs", config_path)
        return None

    process = AgentServerProcess(
        config_path=config_path,
        cwd=config_path.parent,
        graph_names=graphs,
        port=EPHEMERAL_PORT if port is None else port,
        timeout=timeout,
    )
    started = time.monotonic()
    try:
        # Only reached when this is really going to launch something, which is
        # why the progress line lives here rather than at every call site: a
        # Docker user sees nothing.
        from novacode_cli.config.config import boot_status

        boot_status("starting a local agent server (first run can take a minute)")
        await process.start()
    except Exception as exc:  # noqa: BLE001 — a launch failure is never fatal
        process.stop()
        logger.warning("could not start an agent server (%s); using in-process subagents", exc)
        return None

    # Point the existing client at it: it resolves ASYNC_AGENT_BASE_URL when it
    # builds each spec, and the availability probe has already been refreshed to
    # True above, so the specs will be offered.
    os.environ["ASYNC_AGENT_BASE_URL"] = process.url
    with _server_lock:
        _state["process"] = process
    logger.info(
        "agent server ready at %s after %.1fs (log: %s)",
        process.url,
        time.monotonic() - started,
        process.log_path,
    )
    return process


def agent_server_status() -> dict[str, object]:
    """What the launcher currently knows, for ``/agents`` and diagnostics."""
    with _server_lock:
        process = _state["process"]
    if process is None:
        return {"running": False, "url": None, "log_path": None, "graphs": []}
    return {
        "running": process.running,
        "url": process.url,
        "log_path": str(process.log_path) if process.log_path else None,
        "graphs": list(process.graph_names),
    }


def shutdown_agent_server() -> Path | None:
    """Stop any server this session launched; returns its log path if kept."""
    with _server_lock:
        process, _state["process"] = _state["process"], None
    if process is None:
        return None
    path = process.log_path
    with contextlib.suppress(Exception):
        process.stop()
    if path is not None and path.exists():
        return path
    return None
