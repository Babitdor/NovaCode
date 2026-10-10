"""Launch a local ``langgraph dev`` server so async subagents work without Docker.

Nova's dynamic (remote) subagents talk Agent Protocol to a LangGraph server
(`agents/default_subagents/async_subagents.py`). That server used to be a Docker
container, which meant a daemon to keep running and agents that could only see
the one directory mounted into it. This module is the server now: it starts the
graphs in ``agents/async_agents/`` locally, on the first dispatch, working
in the session's project, and points the client at it.

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
#: well-known 2024 that a user's own server uses.
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
#: ``process`` is a server this session started; ``planned`` is one it will start
#: on the first async dispatch (see :func:`plan_agent_server`).
_state: dict[str, AgentServerProcess | None] = {"process": None, "planned": None}
_server_lock = threading.Lock()


def server_extra_available() -> bool:
    """Whether the ``agents-server`` extra (``langgraph-cli``) is installed.

    A missing extra is the normal state for most installs, so callers should
    treat ``False`` as "nothing to do" rather than an error.
    """
    import importlib.util

    return importlib.util.find_spec("langgraph_cli") is not None


def async_server_env() -> dict[str, str]:
    """The stored async-role model, as the environment the graphs read.

    Imported lazily: this module is on the startup path, and the config layer is
    only needed once a server is actually being spawned.
    """
    from novacode_cli.config.role_models import async_server_env as _env

    return _env()


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

    ``--allow-blocking`` is required for MCP tools to work at all. The server
    installs ``blockbuster``, which raises on a blocking call made on the event
    loop, and spawning a stdio MCP server does exactly that: the ``mcp`` library
    resolves the command with ``shutil.which``, which calls ``os.access``. Every
    MCP tool call then fails with ``Blocking call to os.access`` and takes the
    server down with it. The call cannot be moved off the loop, because the
    session it creates is bound to the loop that spawned it.

    It must be the flag, not ``LANGGRAPH_ALLOW_BLOCKING``: ``langgraph_cli``
    writes that variable into the child environment from this flag with
    ``default=False``, so setting it ourselves is overwritten.
    """
    return [
        sys.executable,
        "-m",
        "langgraph_cli",
        "dev",
        "--allow-blocking",
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
    # Nova's stored model for the async role, unless the user already set that
    # variable: an explicit environment variable is the more specific instruction,
    # and either way it is the environment the graphs read at import.
    for key, value in async_server_env().items():
        env.setdefault(key, value)
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
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                msg = (
                    f"agent server exited during startup (code {process.returncode})"
                    f"{_log_tail(read_log)}"
                )
                raise RuntimeError(msg)
            try:
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                msg = f"agent server exited before graph '{graph}' was ready{_log_tail(read_log)}"
                raise RuntimeError(msg)
            try:
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
        workspace: Path | None = None,
    ) -> None:
        """Remember what to launch; nothing is started until :meth:`start`."""
        #: The project the graphs work in (see ``agents/async_workspace.py``).
        #: The server still runs from ``cwd``, where its config lives.
        self.workspace = Path(workspace) if workspace is not None else None
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
            env=build_env({"NOVA_WORKSPACE_ROOT": str(self.workspace)} if self.workspace else None),
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
        await asyncio.to_thread(self.stop)


# ── the session-level policy ────────────────────────────────────────────────


def _config_path() -> Path:
    """The ``langgraph.json`` registering the async graphs.

    It lives inside the package, beside the graphs, so an installed Nova (a uv
    tool, a wheel) can launch the server too, not only a checkout of the repo.

    Only the *shipped* graphs. A user who created an async agent needs a config
    that also registers their graphs, which cannot live in the installed
    package: see :func:`_user_graph_config`.
    """
    return Path(__file__).resolve().parent / "async_agents" / "langgraph.json"


def _graph_names(config_path: Path) -> list[str]:
    """Graph names from ``langgraph.json``, in file order."""
    import json

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config just means no graphs
        return []
    graphs = data.get("graphs") if isinstance(data, dict) else None
    return list(graphs) if isinstance(graphs, dict) else []


#: The generated config's directory, held for the process lifetime so the server
#: can be started, stopped and started again without the directory disappearing
#: under it. ``None`` until a user async agent forces one to exist.
_user_config_dir: tempfile.TemporaryDirectory[str] | None = None

#: Superseded generated directories, kept alive until the session ends.
#:
#: A ``TemporaryDirectory`` deletes itself when it is garbage collected, so a
#: rotated-out directory has to be held onto deliberately: a plan made on another
#: thread may still hold its path and must be able to spawn from it.
_retired_user_config_dirs: list[tempfile.TemporaryDirectory[str]] = []


def _user_graph_config() -> tuple[Path, list[str]] | None:
    """A generated config registering the shipped graphs *and* the user's, or ``None``.

    ``None`` when the user has no async agents, which is the common case and
    leaves the shipped config in use byte for byte.

    The generated config lives in a temp directory rather than beside the
    package's own because the package directory is not Nova's to write: it is
    read-only once installed and overwritten on upgrade. See
    ``agents/async_agents/_config.py`` for the format.
    """
    global _user_config_dir  # noqa: PLW0603 — one dir per process, by design

    from novacode_cli.agents.async_agents import _config
    from novacode_cli.agents.user_async_agents import collect_user_async_agents

    try:
        user_agents = collect_user_async_agents()
    except Exception:  # noqa: BLE001 — a bad agent file must not stop the launch
        logger.warning("could not collect user async agents; using the shipped ones", exc_info=True)
        return None
    if not user_agents:
        return None

    try:
        if _user_config_dir is None:
            _user_config_dir = tempfile.TemporaryDirectory(prefix="nova_user_async_")
        config_path, graph_names = _config.generate(user_agents, Path(_user_config_dir.name))
    except Exception:  # noqa: BLE001 — fall back to the shipped graphs, still serving
        logger.warning(
            "could not generate an async agent config; using shipped graphs", exc_info=True
        )
        return None
    logger.info("agent server will also serve %d user async agent(s)", len(user_agents))
    return config_path, graph_names


def notify_user_async_agents_changed() -> None:
    """Forget the generated config so the next launch rebuilds it.

    Called when a user async agent is created or deleted. A running server has
    already registered the previous set, and its generated directory is
    rewritten under it, so the server is stopped as well: the next dispatch
    starts a new one that serves the current set. With no user agents left there
    is nothing to regenerate and the shipped config comes back on its own.

    The previous directory is left on disk for the interpreter to reap rather
    than deleted here, because a plan made concurrently on another thread may
    still be holding its path.
    """
    # Rotate to a *new* directory rather than deleting the old one. A create or
    # delete runs on a TUI worker while `_plan` may hold a generated path it has
    # not yet spawned from, and on Windows a directory that is about to become a
    # live process's cwd cannot be removed at all.
    #
    # The retired TemporaryDirectory is parked in _retired_user_config_dirs, not
    # left to fall out of scope: TemporaryDirectory deletes itself when it is
    # garbage collected, so dropping the reference here would remove the very
    # directory a pending plan may still be holding.
    global _user_config_dir  # noqa: PLW0603 — one dir per process, by design

    with _server_lock:
        process = _state["process"]
        _state["process"] = None
        _state["planned"] = None
    if process is not None:
        with contextlib.suppress(Exception):
            process.stop()

    stale, _user_config_dir = _user_config_dir, None
    if stale is not None:
        logger.debug("user async agents changed; rotating the generated config directory")
        _retired_user_config_dirs.append(stale)

    # Plan again immediately. `ensure_started` treats "nothing planned" as "an
    # external server is in use" and returns True without starting anything, so
    # simply clearing the plan would leave the next dispatch talking to a server
    # that was never launched. Re-planning reserves the new port the rebuilt
    # config's specs will name.
    with contextlib.suppress(Exception):
        plan_agent_server()
    logger.debug("user async agents changed; the next launch rebuilds the graph config")


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

    Eager: plans and starts in one go, for ``/agent-server start``. Boot uses
    :func:`plan_agent_server` instead and lets the first dispatch start it.
    """
    if not enabled:
        return None

    process = _plan(port=port, timeout=timeout)
    if process is None:
        return None
    with _server_lock:
        _state["planned"] = process
    return process if await ensure_started(announce=True) else None


def _session_workspace() -> Path | None:
    try:
        from novacode_cli.config.config import settings

        return Path(settings.get_workspace_root()).resolve()
    except Exception:  # noqa: BLE001 — without a workspace the server uses its own dir
        return None


def _plan(*, port: int | None = None, timeout: float = HEALTH_TIMEOUT) -> AgentServerProcess | None:
    """The server this session should launch, or ``None`` when it needs none.

    ``None`` when a server that works on this workspace already answers (one the
    user started and declared with ``NOVA_ASYNC_AGENT_ROOT``), or when the
    optional extra is missing. A server that answers but is rooted at
    another project does not count: its agents would read the wrong files.
    """
    from novacode_cli.agents.default_subagents.async_subagents import (
        async_agents_available,
        async_agents_see_workspace,
    )

    with _server_lock:
        if _state["process"] is not None or _state["planned"] is not None:
            return _state["process"] or _state["planned"]
    if async_agents_available(refresh=True) and async_agents_see_workspace():
        logger.debug("an agent server for this workspace is already reachable")
        return None

    if not server_extra_available():
        logger.info(
            "async agents need a LangGraph server: install the optional extra "
            "(uv sync --extra agents-server) so Nova can launch one for this project"
        )
        return None

    config_path = _config_path()
    graphs = _graph_names(config_path)
    # A user's async agents cannot be registered in the package's own config, so
    # when there are any the launcher serves a generated one instead — same
    # graphs plus theirs.
    generated = _user_graph_config()
    if generated is not None:
        config_path, graphs = generated
    if not config_path.is_file() or not graphs:
        logger.warning("cannot launch an agent server: %s has no graphs", config_path)
        return None

    return AgentServerProcess(
        config_path=config_path,
        cwd=config_path.parent,
        graph_names=graphs,
        # Reserved now, not at spawn: the specs are built with the URL long
        # before the server exists.
        port=find_free_port(DEFAULT_HOST) if port is None else port,
        timeout=timeout,
        workspace=_session_workspace(),
    )


def plan_agent_server(*, enabled: bool = True, port: int | None = None) -> bool:
    """Decide where async agents will run, without starting anything.

    Called at boot. Reserves a port and points the client at it, so the async
    tools are offered at once; the server itself comes up on the first
    ``start_async_task`` (``agents/async_on_demand.py``). Returns whether a
    launch was planned.
    """
    if not enabled:
        return False
    try:
        process = _plan(port=port)
    except Exception:  # noqa: BLE001 — planning must never break start-up
        logger.debug("could not plan an agent server", exc_info=True)
        return False
    if process is None:
        return False
    os.environ["ASYNC_AGENT_BASE_URL"] = process.url
    with _server_lock:
        _state["planned"] = process
    return True


def planned() -> bool:
    """Whether this session has a server planned or running (i.e. its own)."""
    with _server_lock:
        return _state["planned"] is not None or _state["process"] is not None


async def ensure_started(*, announce: bool = False) -> bool:
    """Bring the planned server up. ``True`` when async agents can be reached.

    ``True`` at once when nothing was planned (an external server is in use) or
    it is already running. A failed launch returns ``False`` and leaves the plan
    in place, so a later dispatch can try again.
    """
    with _server_lock:
        process = _state["planned"] or _state["process"]
    if process is None or process.running:
        return True
    planned_url = process.url
    started = time.monotonic()
    try:
        if announce:
            from novacode_cli.config.config import boot_status

            boot_status("starting a local agent server (first run can take a minute)")
        await process.start()
    except Exception as exc:  # noqa: BLE001 — a launch failure is never fatal
        process.stop()
        logger.warning("could not start an agent server (%s); using in-process subagents", exc)
        return False
    if process.url != planned_url:
        # The reserved port was taken in the meantime. Specs built earlier still
        # name the old one; new ones pick this up.
        logger.warning("agent server moved from %s to %s", planned_url, process.url)
    os.environ["ASYNC_AGENT_BASE_URL"] = process.url
    with _server_lock:
        _state["process"], _state["planned"] = process, None
    logger.info(
        "agent server ready at %s after %.1fs (log: %s)",
        process.url,
        time.monotonic() - started,
        process.log_path,
    )
    return True


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
        _state["planned"] = None
    if process is None:
        return None
    path = process.log_path
    with contextlib.suppress(Exception):
        process.stop()
    if path is not None and path.exists():
        return path
    return None


def cleanup_user_config() -> None:
    """Delete the generated async-agent config directory, if one was made.

    Kept separate from :func:`shutdown_agent_server` because the server is
    stopped and restarted inside a session (``/agent-server restart``), which must
    not throw away the config that is about to be used again. This is for the end
    of the session.
    """
    global _user_config_dir  # noqa: PLW0603 — one dir per process, by design

    stale, _user_config_dir = _user_config_dir, None
    if stale is not None:
        with contextlib.suppress(Exception):
            stale.cleanup()
    # The directories rotated out earlier by notify_user_async_agents_changed can
    # go too: the session is ending, so nothing is holding a path into them.
    while _retired_user_config_dirs:
        retired = _retired_user_config_dirs.pop()
        with contextlib.suppress(Exception):
            retired.cleanup()
