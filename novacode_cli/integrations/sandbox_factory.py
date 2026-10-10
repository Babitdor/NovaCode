"""Sandbox lifecycle management with context managers."""

import atexit
import logging
import os
import shlex
import signal
import string
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    SandboxBackendProtocol,
)
from deepagents.backends.sandbox import BaseSandbox

from novacode_cli.config.config import console

logger = logging.getLogger(__name__)


def parse_ports(ports_str: str | None) -> dict[int, int] | None:
    """Parse port forwarding argument.

    Args:
        ports_str: Port string in format 'PORT' or 'HOST_PORT:CONTAINER_PORT'.
                   Multiple ports separated by comma.
                   Example: '8080,3000:3000,5432:5432'

    Returns:
        Dictionary mapping container ports to host ports, or None if no ports.
        Example: {8080: 8080, 3000: 3000, 5432: 5432}
    """
    if not ports_str:
        return None

    ports = {}
    for port_spec in ports_str.split(","):
        port_spec = port_spec.strip()
        if ":" in port_spec:
            # Format: HOST_PORT:CONTAINER_PORT
            host_port_str, container_port_str = port_spec.split(":", 1)
            host_port = int(host_port_str)
            container_port = int(container_port_str)
        else:
            # Format: PORT (same for both host and container)
            port = int(port_spec)
            host_port = port
            container_port = port
        ports[container_port] = host_port

    return ports if ports else None


def resolve_sandbox_type(
    sandbox_arg: str | None,
    no_sandbox: bool,  # noqa: FBT001
    platform: str | None = None,
) -> tuple[str, bool]:
    """Resolve the effective sandbox mode and whether the user chose it explicitly.

    The default depends on platform. Linux/macOS get Pattern A (``"os"`` — host
    files + an OS-confined shell). Windows has no lightweight OS sandbox primitive,
    so the default is plain host execution (``"none"``) gated by the
    dangerous-command blocklist + HITL approval — the same baseline Claude Code /
    Cursor use on Windows. Docker stays available there as an explicit opt-in
    (``--sandbox docker``); it is Windows-only and rejected elsewhere (Linux/macOS
    should use the ``"os"`` default or ``--no-sandbox``).

    Args:
        sandbox_arg: The ``--sandbox`` value, or None when unset.
        no_sandbox: Whether ``--no-sandbox`` was passed.
        platform: ``sys.platform`` override (for tests). Defaults to the host.

    Returns:
        ``(sandbox_type, explicit)`` — ``explicit`` is True when the user chose
        the mode (so a creation failure should not silently fall back).

    Raises:
        ValueError: On invalid combinations (caller maps these to a user error).
    """
    is_windows = (platform or sys.platform) == "win32"

    if no_sandbox:
        if sandbox_arg and sandbox_arg != "none":
            msg = "--no-sandbox conflicts with --sandbox."
            raise ValueError(msg)
        return "none", True

    if sandbox_arg is not None:
        if sandbox_arg == "docker" and not is_windows:
            msg = (
                "--sandbox docker is Windows-only. On Linux/macOS, Nova confines the "
                "shell with an OS sandbox by default (--sandbox os). Use --no-sandbox "
                "for unconfined local execution."
            )
            raise ValueError(msg)
        return sandbox_arg, True

    # Implicit default: host execution + approvals on Windows (no lightweight OS
    # sandbox primitive — Docker is opt-in via --sandbox docker); Pattern A
    # (OS-confined shell) on Linux/macOS.
    return ("none" if is_windows else "os"), False


# Only these env vars are allowed in setup script template substitution.
# Prevents accidental leakage of API keys and secrets.
_ALLOWED_SETUP_VARS = {"HOME", "USER", "PATH", "SHELL", "LANG", "LC_ALL", "TERM"}

# Default base image for the Docker sandbox. Overridable via NOVA_SANDBOX_IMAGE.
# python:*-slim is intentionally minimal — it ships WITHOUT git, build tools, or
# linters — so a freshly-created container is provisioned with the baseline
# toolchain below before the agent runs (see _provision_sandbox_tools).
_DEFAULT_SANDBOX_IMAGE = "python:3.11-slim"

# Baseline toolchain installed into a freshly-created sandbox so the agent can
# actually run version control, linting, tests, and builds. Without these,
# execute/test/validate commands fail almost immediately (git/ruff "not found").
#
#   - git: version control (clone, status, diff).
#   - ca-certificates: SSL certs for HTTPS clones and fetches.
#   - curl: HTTP fetches from shell (covers wget functionality).
#   - ripgrep: fast code search (rg) the agent uses constantly.
#   - unzip: occasionally needed for dependency extraction.
#
# Dropped (saves ~250MB+ per container):
#   - openssh-client: HTTPS works for git, SSH cloning is rare.
#   - wget: redundant with curl.
#   - build-essential: most wheels are pre-compiled; uv handles edge cases.
#   - nodejs / npm: Python-centric agent rarely uses JS scaffolding.
#   - jq / tree / less: cosmetic utilities, rarely functional.
#   - networkx: only for graph building; agent degrades gracefully.
_PROVISION_APT_PACKAGES = (
    "git",
    "ca-certificates",
    "curl",
    "ripgrep",
    "unzip",
)
# uv is a fast Rust-based pip/venv/workflow replacement.
_PROVISION_PIP_PACKAGES = ("ruff", "pytest", "uv")


def _sandbox_image() -> str:
    """The Docker image to use, honoring the NOVA_SANDBOX_IMAGE override."""
    return os.environ.get("NOVA_SANDBOX_IMAGE", "").strip() or _DEFAULT_SANDBOX_IMAGE


def _skip_provision() -> bool:
    """Whether to skip baseline toolchain provisioning (NOVA_SANDBOX_SKIP_PROVISION).

    Set this when using a pre-baked image that already has git/ruff/etc., to
    avoid the apt/pip install on first container start.
    """
    return os.environ.get("NOVA_SANDBOX_SKIP_PROVISION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _build_provision_script() -> str:
    """Build the idempotent bash script that installs the baseline toolchain.

    - System packages (git + build tools) go through apt, but only when ``git``
      is missing — apt update/install is the slow part, and git is the canary.
    - Python tools (ruff, pytest, uv) go through pip in one satisfiable call.
    - Each step is best-effort: a failure prints a marker and continues rather
      than aborting the whole session. A final summary line lets the run log show
      exactly which tools ended up available.

    Extra packages can be appended via NOVA_SANDBOX_EXTRA_APT / NOVA_SANDBOX_EXTRA_PIP
    (space-separated).
    """
    apt_pkgs = list(_PROVISION_APT_PACKAGES) + shlex.split(
        os.environ.get("NOVA_SANDBOX_EXTRA_APT", "")
    )
    pip_pkgs = list(_PROVISION_PIP_PACKAGES) + shlex.split(
        os.environ.get("NOVA_SANDBOX_EXTRA_PIP", "")
    )
    apt_list = " ".join(shlex.quote(p) for p in apt_pkgs)
    pip_list = " ".join(shlex.quote(p) for p in pip_pkgs)

    return f"""set -u
export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq --no-install-recommends {apt_list} || echo "nova-provision: apt install failed (continuing)"
    rm -rf /var/lib/apt/lists/* 2>/dev/null || true
  fi
fi
if command -v pip >/dev/null 2>&1; then
  pip install --quiet --no-input --root-user-action=ignore {pip_list} || echo "nova-provision: pip install failed (continuing)"
fi
echo "nova-provision-summary: git=$(command -v git || echo MISSING) rg=$(command -v rg || echo MISSING) ruff=$(command -v ruff || echo MISSING) pytest=$(command -v pytest || echo MISSING) uv=$(command -v uv || echo MISSING)"
"""


def _provision_sandbox_tools(backend: SandboxBackendProtocol) -> None:
    """Install the baseline toolchain into a freshly-created sandbox.

    Best-effort: warns (but does not abort the session) if some tools couldn't
    be installed, so the user knows execute/test commands may be degraded and
    can point NOVA_SANDBOX_IMAGE at a richer image instead.
    """
    console.print("[dim]Provisioning sandbox toolchain (git, rg, ruff, pytest, uv, unzip)...[/dim]")
    try:
        # execute() already runs the command under `bash -c`; pass the script raw.
        result = backend.execute(_build_provision_script(), timeout=600)
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]⚠ Sandbox provisioning failed: {e}[/yellow]")
        return

    out = (result.output or "").strip()
    summary = next(
        (ln for ln in out.splitlines() if ln.startswith("nova-provision-summary:")),
        "",
    )
    if result.exit_code != 0 or "MISSING" in summary:
        console.print(
            f"[yellow]⚠ Sandbox toolchain incomplete[/yellow] [dim]{summary or out[-300:]}[/dim]"
        )
        console.print(
            "[dim]  Some tools may be unavailable. Set NOVA_SANDBOX_IMAGE to a "
            "pre-baked image, or set NOVA_SANDBOX_EXTRA_APT/NOVA_SANDBOX_EXTRA_PIP "
            "for additional packages.[/dim]"
        )
    else:
        console.print("[green]✓ Sandbox toolchain ready (git, rg, ruff, pytest, uv)[/green]")


def _await_sandbox_ready(
    poll_fn: Callable[[], bool],
    *,
    timeout: float = 180.0,
    interval: float = 2.0,
    on_timeout: Callable[[], None] | None = None,
) -> None:
    """Poll until the sandbox is ready or a timeout elapses.

    Args:
        poll_fn: A callable that returns True when the sandbox is ready,
            False if it's not ready yet. May raise RuntimeError for
            terminal failures (e.g. sandbox terminated unexpectedly).
        timeout: Maximum time in seconds to wait.
        interval: Seconds between polls.
        on_timeout: Optional cleanup callable invoked before raising
            the timeout error (e.g. to terminate a half-started sandbox).

    Raises:
        RuntimeError: If the sandbox did not become ready within the timeout.
    """
    max_attempts = int(timeout / interval)
    for _ in range(max_attempts):
        try:
            if poll_fn():
                return
        except RuntimeError:
            raise  # Terminal failure — propagate immediately
        except Exception:
            pass  # Transient error — retry
        time.sleep(interval)
    # Timeout — run cleanup (if any) then raise
    if on_timeout:
        on_timeout()
    msg = f"Sandbox failed to start within {timeout:.0f} seconds"
    raise RuntimeError(msg)


def _run_sandbox_setup(backend: SandboxBackendProtocol, setup_script_path: str) -> None:
    """Run users setup script in sandbox with env var expansion.

    Args:
        backend: Sandbox backend instance
        setup_script_path: Path to setup script file
    """
    script_path = Path(setup_script_path).resolve()

    # Prevent path traversal - script must be under current working directory or home
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    if not (script_path.is_relative_to(cwd) or script_path.is_relative_to(home)):
        msg = f"Setup script must be under working directory or home: {setup_script_path}"
        raise ValueError(msg)

    if not script_path.exists():
        msg = f"Setup script not found: {setup_script_path}"
        raise FileNotFoundError(msg)

    console.print(f"[dim]Running setup script: {setup_script_path}...[/dim]")

    # Read script content
    script_content = script_path.read_text()

    # Expand ${VAR} syntax using only allowlisted environment variables
    template = string.Template(script_content)
    safe_env = {k: v for k, v in os.environ.items() if k in _ALLOWED_SETUP_VARS}
    expanded_script = template.safe_substitute(safe_env)

    # Execute in sandbox with 5-minute timeout
    result = backend.execute(f"bash -c {shlex.quote(expanded_script)}")

    if result.exit_code != 0:
        console.print(f"[red]❌ Setup script failed (exit {result.exit_code}):[/red]")
        console.print(f"[dim]{result.output}[/dim]")
        msg = "Setup failed - aborting"
        raise RuntimeError(msg)

    console.print("[green]✓ Setup complete[/green]")


def _modal_is_ready(sandbox) -> bool:
    """Check if a Modal sandbox is ready to accept commands."""
    if sandbox.poll() is not None:  # Sandbox terminated unexpectedly
        msg = "Modal sandbox terminated unexpectedly during startup"
        raise RuntimeError(msg)
    try:
        process = sandbox.exec("echo", "ready", timeout=5)
        process.wait()
        return process.returncode == 0
    except Exception:
        return False


@contextmanager
def create_modal_sandbox(
    *, sandbox_id: str | None = None, setup_script_path: str | None = None
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create or connect to Modal sandbox.

    Args:
        sandbox_id: Optional existing sandbox ID to reuse
        setup_script_path: Optional path to setup script to run after sandbox starts

    Yields:
        (ModalBackend, sandbox_id)

    Raises:
        ImportError: Modal SDK not installed
        Exception: Sandbox creation/connection failed
        FileNotFoundError: Setup script not found
        RuntimeError: Setup script failed
    """
    import modal

    from novacode_cli.integrations.modal import ModalBackend

    console.print("[yellow]Starting Modal sandbox...[/yellow]")

    # Create ephemeral app (auto-cleans up on exit)
    app = modal.App("deepagents-sandbox")

    with app.run():
        sandbox = None
        should_cleanup = sandbox_id is None
        try:
            if sandbox_id:
                sandbox = modal.Sandbox.from_id(sandbox_id=sandbox_id)
            else:
                sandbox = modal.Sandbox.create(app=app, workdir="/workspace")
                _await_sandbox_ready(lambda: _modal_is_ready(sandbox))

            backend = ModalBackend(sandbox)
            console.print(f"[green]✓ Modal sandbox ready: {backend.id}[/green]")
            if setup_script_path:
                _run_sandbox_setup(backend, setup_script_path)
            yield backend
        finally:
            if should_cleanup and sandbox is not None:
                try:
                    console.print(
                        f"[dim]Terminating Modal sandbox {getattr(sandbox, 'object_id', sandbox_id)}...[/dim]"
                    )
                    sandbox.terminate()
                    console.print("[dim]✓ Modal sandbox terminated[/dim]")
                except Exception as e:
                    console.print(f"[yellow]⚠ Cleanup failed: {e}[/yellow]")


def _runloop_is_ready(client, devbox_id: str) -> bool:
    """Check if a Runloop devbox is in 'running' status."""
    status = client.devboxes.retrieve(id=devbox_id)
    return status.status == "running"


@contextmanager
def create_runloop_sandbox(
    *, sandbox_id: str | None = None, setup_script_path: str | None = None
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create or connect to Runloop devbox.

    Args:
        sandbox_id: Optional existing devbox ID to reuse
        setup_script_path: Optional path to setup script to run after sandbox starts

    Yields:
        (RunloopBackend, devbox_id)

    Raises:
        ImportError: Runloop SDK not installed
        ValueError: RUNLOOP_API_KEY not set
        RuntimeError: Devbox failed to start within timeout
        FileNotFoundError: Setup script not found
        RuntimeError: Setup script failed
    """
    from runloop_api_client import Runloop

    from novacode_cli.integrations.runloop import RunloopBackend

    bearer_token = os.environ.get("RUNLOOP_API_KEY")
    if not bearer_token:
        msg = "RUNLOOP_API_KEY environment variable not set"
        raise ValueError(msg)

    client = Runloop(bearer_token=bearer_token)

    console.print("[yellow]Starting Runloop devbox...[/yellow]")

    devbox = None
    should_cleanup = sandbox_id is None
    try:
        if sandbox_id:
            devbox = client.devboxes.retrieve(id=sandbox_id)
        else:
            devbox = client.devboxes.create()
            sandbox_id = devbox.id
            _await_sandbox_ready(lambda: _runloop_is_ready(client, sandbox_id))

        console.print(f"[green]✓ Runloop devbox ready: {devbox.id}[/green]")
        backend = RunloopBackend(devbox_id=devbox.id, client=client)
        if setup_script_path:
            _run_sandbox_setup(backend, setup_script_path)
        yield backend
    finally:
        if should_cleanup and devbox is not None:
            try:
                console.print(f"[dim]Shutting down Runloop devbox {devbox.id}...[/dim]")
                client.devboxes.shutdown(id=devbox.id)
                console.print(f"[dim]✓ Runloop devbox {devbox.id} terminated[/dim]")
            except Exception as e:
                console.print(f"[yellow]⚠ Cleanup failed: {e}[/yellow]")


def _daytona_is_ready(sandbox) -> bool:
    """Check if a Daytona sandbox can execute commands."""
    try:
        result = sandbox.process.exec("echo ready", timeout=5)
        return result.exit_code == 0
    except Exception:
        return False


@contextmanager
def create_daytona_sandbox(
    *, sandbox_id: str | None = None, setup_script_path: str | None = None
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create Daytona sandbox.

    Args:
        sandbox_id: Optional existing sandbox ID to reuse
        setup_script_path: Optional path to setup script to run after sandbox starts

    Yields:
        (DaytonaBackend, sandbox_id)

    Note:
        Connecting to existing Daytona sandbox by ID may not be supported yet.
        If sandbox_id is provided, this will raise NotImplementedError.
    """
    from daytona import Daytona, DaytonaConfig

    from novacode_cli.integrations.daytona import DaytonaBackend

    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        msg = "DAYTONA_API_KEY environment variable not set"
        raise ValueError(msg)

    if sandbox_id:
        msg = (
            "Connecting to existing Daytona sandbox by ID not yet supported. "
            "Create a new sandbox by omitting --sandbox-id."
        )
        raise NotImplementedError(msg)

    console.print("[yellow]Starting Daytona sandbox...[/yellow]")

    daytona = Daytona(DaytonaConfig(api_key=api_key))
    sandbox = None
    try:
        sandbox = daytona.create()
        sandbox_id = sandbox.id
        _await_sandbox_ready(lambda: _daytona_is_ready(sandbox))
        backend = DaytonaBackend(sandbox)
        console.print(f"[green]✓ Daytona sandbox ready: {backend.id}[/green]")
        if setup_script_path:
            _run_sandbox_setup(backend, setup_script_path)
        yield backend
    finally:
        if sandbox is not None:
            console.print(f"[dim]Deleting Daytona sandbox {sandbox.id}...[/dim]")
            try:
                sandbox.delete()
                console.print(f"[dim]✓ Daytona sandbox {sandbox.id} terminated[/dim]")
            except Exception as e:
                console.print(f"[yellow]⚠ Cleanup failed: {e}[/yellow]")


@contextmanager
def create_langsmith_sandbox(  # noqa: PLR0912, PLR0915
    *,
    sandbox_id: str | None = None,
    setup_script_path: str | None = None,
    vcpus: int | None = None,
    mem_bytes: int | None = None,
    fs_capacity_bytes: int | None = None,
    snapshot_id: str | None = None,
    snapshot_name: str | None = None,
    ports: dict[int, int] | None = None,
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create LangSmith sandbox.

    Args:
        sandbox_id: Optional existing sandbox name to reuse
        setup_script_path: Optional path to setup script to run after sandbox starts
        vcpus: Number of virtual CPUs for the sandbox
        mem_bytes: Memory in bytes for the sandbox
        fs_capacity_bytes: Filesystem capacity in bytes
        snapshot_id: Snapshot ID to boot from (mutually exclusive with snapshot_name)
        snapshot_name: Snapshot name to boot from (resolved server-side)
        ports: Port mappings for tunnel creation {container_port: host_port}

    Yields:
        SandboxBackendProtocol instance.

    Note:
        LangSmith sandboxes can be reconnected to by name (get_sandbox).
        If the sandbox was previously stopped, it will be started automatically.
    """
    from langsmith.sandbox import SandboxClient

    from novacode_cli.integrations.langsmith import LangSmithBackend

    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    if not api_key:
        msg = (
            "LANGSMITH_API_KEY environment variable not set. "
            "Get your API key from https://smith.langchain.com"
        )
        raise ValueError(msg)

    client = SandboxClient()
    sandbox = None
    should_cleanup = sandbox_id is None
    started_existing = False
    tunnels: list[object] = []

    try:
        if sandbox_id:
            console.print(f"[yellow]Reconnecting to LangSmith sandbox: {sandbox_id}[/yellow]")
            sandbox = client.get_sandbox(sandbox_id)
            if sandbox.status == "stopped":
                console.print("[yellow]Sandbox is stopped. Starting...[/yellow]")
                sandbox.start()
                started_existing = True
        else:
            console.print("[yellow]Creating LangSmith sandbox...[/yellow]")

            create_kwargs: dict = {
                "timeout": 120,
                "idle_ttl_seconds": 0,  # Disable idle auto-stop
            }
            if snapshot_id:
                create_kwargs["snapshot_id"] = snapshot_id
            if snapshot_name:
                create_kwargs["snapshot_name"] = snapshot_name
            if vcpus is not None:
                create_kwargs["vcpus"] = vcpus
            if mem_bytes is not None:
                create_kwargs["mem_bytes"] = mem_bytes
            if fs_capacity_bytes is not None:
                create_kwargs["fs_capacity_bytes"] = fs_capacity_bytes
            if snapshot_id and snapshot_name:
                msg = "snapshot_id and snapshot_name are mutually exclusive"
                raise ValueError(msg)

            sandbox = client.create_sandbox(**create_kwargs)

            _resource_summary_parts = []
            if vcpus is not None:
                _resource_summary_parts.append(f"{vcpus} vCPU")
            if mem_bytes is not None:
                _resource_summary_parts.append(f"{mem_bytes // (1024**3)}GB RAM")
            if fs_capacity_bytes is not None:
                _resource_summary_parts.append(f"{fs_capacity_bytes // (1024**3)}GB disk")
            if snapshot_id or snapshot_name:
                _resource_summary_parts.append(f"snapshot: {snapshot_name or snapshot_id}")
            if _resource_summary_parts:
                console.print(f"[dim]  {', '.join(_resource_summary_parts)}[/dim]")

        sandbox_id = sandbox.name
        backend = LangSmithBackend(sandbox)
        console.print(f"[green]✓ LangSmith sandbox ready: {backend.id}[/green]")

        # Attach metadata for the startup banner display
        meta: dict[str, object] = {
            "id": backend.id,
            "provider": "langsmith",
        }
        if vcpus is not None:
            meta["vcpus"] = vcpus
        if mem_bytes is not None:
            meta["mem_bytes"] = mem_bytes
            meta["mem_gb"] = f"{mem_bytes // (1024**3)}GB"
        if fs_capacity_bytes is not None:
            meta["fs_capacity_bytes"] = fs_capacity_bytes
            meta["fs_capacity_gb"] = f"{fs_capacity_bytes // (1024**3)}GB"
        if snapshot_id:
            meta["snapshot"] = snapshot_id
        if snapshot_name:
            meta["snapshot"] = snapshot_name
        backend._nova_meta = meta  # type: ignore[attr-defined]

        # Create tunnels for port forwarding
        tunnels_list: list[dict[str, int]] = []
        if ports:
            for container_port, host_port in ports.items():
                try:
                    tunnel = sandbox.tunnel(container_port, local_port=host_port)
                    tunnels.append(tunnel)
                    tunnels_list.append({"host": host_port, "container": container_port})
                    console.print(
                        f"[dim]Tunnel: localhost:{host_port} -> sandbox:{container_port}[/dim]"
                    )
                except Exception as e:  # noqa: BLE001
                    console.print(
                        f"[yellow]⚠ Tunnel for port {container_port} failed: {e}[/yellow]"
                    )

        if tunnels_list:
            meta["tunnels"] = tunnels_list

        # Run setup script if provided
        if setup_script_path:
            _run_sandbox_setup(backend, setup_script_path)

        yield backend

    finally:
        for tunnel in reversed(tunnels):
            try:
                tunnel.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("Could not close LangSmith tunnel: %s", e)
        if sandbox is not None and should_cleanup:
            console.print(f"[dim]Deleting LangSmith sandbox {sandbox_id}...[/dim]")
            try:
                sandbox.delete()
                console.print("[dim]✓ LangSmith sandbox terminated[/dim]")
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]⚠ Cleanup failed: {e}[/yellow]")
        elif sandbox is not None and started_existing:
            try:
                sandbox.stop()
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]⚠ Could not stop reconnected sandbox: {e}[/yellow]")
        try:
            client.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not close LangSmith client: %s", e)


def _saved_session_ids() -> set[str]:
    """Return the set of session ids that still exist on disk."""
    from pathlib import Path

    sessions_dir = Path.home() / ".nova" / "sessions"
    try:
        return {p.name for p in sessions_dir.iterdir() if p.is_dir()}
    except OSError:
        return set()


def _cleanup_stale_docker_containers(
    client, *, keep_id: str | None = None, max_age_days: int = 30
) -> None:
    """Prune Nova-managed Docker containers that won't be reconnected.

    Persisted containers are stopped (not removed) on exit so sessions can
    reconnect. This best-effort startup sweep removes two kinds of leftovers so
    they don't accumulate:

    * **Orphans** — a managed container whose ``nova.session`` no longer has a
      session saved on disk (e.g. the user started Nova, it created the sandbox,
      then exited before any turn was saved). Removed regardless of age, but only
      when *not running* so a concurrent live session is never killed.
    * **Stale** — anything older than ``max_age_days``.

    The container currently being reconnected (``keep_id``) is never removed.

    Args:
        client: Docker client
        keep_id: Container ID to exclude from cleanup (the one we're reusing)
        max_age_days: Age threshold; containers created before this are removed
    """
    import time
    from datetime import datetime

    cutoff = time.time() - max_age_days * 86400
    try:
        managed = client.containers.list(all=True, filters={"label": "nova.managed=1"})
    except Exception:  # noqa: BLE001
        return  # Docker API hiccup — skip cleanup silently

    valid_ids = _saved_session_ids()

    # A container re-tied by /clear keeps its original (now-stale) Docker label,
    # so the label-based orphan test below would wrongly target it. The registry
    # is the authoritative owner: protect any container it still owns for a valid
    # session (match by id prefix, since backend.id may be short or full).
    try:
        from novacode_cli.integrations import sandbox_registry

        protected_ids = [
            str(rec.get("sandbox_id", ""))
            for rec in sandbox_registry.list_records()
            if rec.get("session_id", "") in valid_ids and rec.get("sandbox_id")
        ]
    except Exception:  # noqa: BLE001
        protected_ids = []

    def _is_protected(container_id: str) -> bool:
        return any(
            container_id.startswith(pid) or pid.startswith(container_id[:12])
            for pid in protected_ids
        )

    for cont in managed:
        try:
            if keep_id and (cont.id == keep_id or cont.id.startswith(keep_id)):
                continue
            if _is_protected(cont.id):
                continue

            # Orphan: its session was never saved (or was deleted) and it isn't
            # running, so nothing will ever reconnect it. Remove immediately.
            session_label = (getattr(cont, "labels", None) or {}).get("nova.session") or ""
            if (
                session_label
                and session_label not in valid_ids
                and getattr(cont, "status", "") != "running"
            ):
                cont.remove(force=True)
                console.print(
                    f"[dim]Removed orphaned sandbox container {cont.id[:12]} "
                    f"(no saved session)[/dim]"
                )
                continue

            created_raw = cont.attrs.get("Created", "")
            # Docker timestamps look like 2026-05-30T12:00:00.000000000Z
            created_str = created_raw.split(".")[0].rstrip("Z")
            created_ts = datetime.fromisoformat(created_str).timestamp()
            if created_ts < cutoff:
                cont.remove(force=True)
                console.print(
                    f"[dim]Removed stale sandbox container {cont.id[:12]} "
                    f"(age > {max_age_days}d)[/dim]"
                )
        except Exception:  # noqa: BLE001, S112
            continue  # Best effort — never block startup on cleanup


def _keep_sandbox_on_exit(persist: bool, backend: object) -> bool:  # noqa: FBT001
    """Whether to keep a persistable sandbox on exit.

    ``persist`` is the requested policy; a caller can still veto it by setting
    ``backend._nova_discard_on_exit = True`` (e.g. when the session saved
    nothing, so keeping the container would just orphan it).
    """
    return bool(persist) and not getattr(backend, "_nova_discard_on_exit", False)


@contextmanager
def create_docker_sandbox(
    *,
    sandbox_id: str | None = None,
    setup_script_path: str | None = None,
    ports: dict[int, int] | None = None,
    mount_dir: str | None = None,
    persist: bool = False,
    session_id: str | None = None,
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create or connect to Docker container sandbox.

    Args:
        sandbox_id: Optional existing container ID to reuse. If the container no
               longer exists, a fresh one is created (and the project re-mounted).
        setup_script_path: Optional path to setup script to run after container
               starts. Only runs for freshly-created containers (skipped on
               reconnect, where dependencies already persist).
        ports: Optional port mapping as {container_port: host_port}.
               Example: {8080: 8080, 3000: 3000} maps container ports to same host ports.
               If None, no ports are exposed.
        mount_dir: Optional host directory to bind-mount into the container at
               /workspace (read-write). When set, the agent operates on the real
               project files (edits write through to disk) while running isolated
               inside the container. Used only when creating a container; a
               reconnected container keeps its original mount.
        persist: When True, the container is stopped (not removed) on exit so a
               later session can reconnect to it, preserving installed packages
               and other in-container state. When False, freshly-created
               containers are removed on exit (ephemeral).
        session_id: Session identifier used to name/label the container
               (nova-<session_id>) so it can be discovered and reconnected.

    Yields:
        DockerBackend instance

    Raises:
        ImportError: Docker SDK not installed
        Exception: Container creation/connection failed
        FileNotFoundError: Setup script not found
        RuntimeError: Setup script failed
    """
    import docker

    from novacode_cli.config.config import boot_status
    from novacode_cli.integrations.docker import DockerBackend

    # Connect to Docker daemon
    try:
        client = docker.from_env()
    except Exception as e:
        msg = f"Failed to connect to Docker daemon: {e}\nIs Docker running?"
        raise RuntimeError(msg) from e

    # Best-effort prune of abandoned managed containers (never the one we reuse).
    _cleanup_stale_docker_containers(client, keep_id=sandbox_id)

    def _create_new_container():
        """Create and start a fresh container; returns the container."""
        console.print("[dim]Creating new container...[/dim]")

        # Base image (NOVA_SANDBOX_IMAGE override). Minimal by default; the
        # baseline toolchain (git/ruff/pytest) is provisioned after start.
        image = _sandbox_image()

        # Pull image if not present
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            console.print(f"[dim]Pulling image {image}...[/dim]")
            client.images.pull(image)

        # Convert port mapping to Docker format
        # {8080: 8080} -> {'8080/tcp': 8080}
        port_bindings = None
        if ports:
            port_bindings = {
                f"{container_port}/tcp": host_port for container_port, host_port in ports.items()
            }
            console.print(f"[dim]Exposing ports: {ports}[/dim]")

        # Bind-mount the host project directory into the container at
        # /workspace (read-write). Edits made by the agent write through
        # to the real project on disk, while execution stays isolated.
        volumes: dict[str, dict[str, str]] = {}
        if mount_dir:
            host_path = str(Path(mount_dir).resolve())
            volumes[host_path] = {"bind": "/workspace", "mode": "rw"}
            console.print(f"[dim]Mounting {host_path} → /workspace (rw)[/dim]")

        # Name + label so the container can be discovered and reconnected, and
        # swept by _cleanup_stale_docker_containers when abandoned.
        name = f"nova-{session_id}" if session_id else None
        labels = {"nova.managed": "1", "nova.session": session_id or ""}

        run_kwargs = dict(
            image=image,
            command="sleep infinity",  # Keep container running
            detach=True,
            working_dir="/workspace",
            volumes=volumes,
            environment={},
            stdin_open=True,
            tty=True,
            ports=port_bindings,  # Port forwarding
            labels=labels,
        )
        try:
            cont = client.containers.run(name=name, **run_kwargs)
        except docker.errors.APIError as e:
            # 409 name conflict: a container by this name already exists —
            # reuse it instead of failing.
            if name and "Conflict" in str(e):
                console.print(f"[dim]Container name {name} in use; reconnecting to it...[/dim]")
                cont = client.containers.get(name)
                if cont.status != "running":
                    cont.start()
                return cont, False
            raise

        # Wait for container to be ready
        import time

        try:
            for _ in range(30):  # 30 second timeout
                cont.reload()
                if cont.status == "running":
                    break
                time.sleep(1)
            else:
                msg = "Docker container failed to start within timeout"
                raise RuntimeError(msg)
        except BaseException:
            with suppress(Exception):
                cont.remove(force=True)
            raise

        return cont, True

    container = None
    backend = None
    created_new = False

    try:
        if sandbox_id:
            # Try to reuse an existing container; self-heal if it's gone.
            try:
                container = client.containers.get(sandbox_id)
                if container.status != "running":
                    container.start()
            except docker.errors.NotFound:
                boot_status(
                    f"sandbox: container {sandbox_id[:12]} gone — creating a fresh one",
                    "warn",
                )
                container, created_new = _create_new_container()
        else:
            container, created_new = _create_new_container()

        backend = DockerBackend(container)
        boot_status(f"sandbox: docker {backend.id[:12]} ready", "ok")

        # Show exposed ports if any
        if ports:
            for container_port, host_port in ports.items():
                console.print(
                    f"[dim]Port forwarding: localhost:{host_port} -> container:{container_port}[/dim]"
                )

        # Provision the baseline toolchain (git, ruff, pytest, build tools) into
        # freshly-created containers so the agent can run version control,
        # linting, tests, and builds. The default python:*-slim image lacks
        # these, which made execute/test/validate commands fail. A reconnected
        # container already has them, so this runs only on fresh create.
        if created_new and not _skip_provision():
            _provision_sandbox_tools(backend)

        # Run setup script only for freshly-created containers — a reconnected
        # container already has its dependencies installed.
        if setup_script_path and created_new:
            _run_sandbox_setup(backend, setup_script_path)

        yield backend

    finally:
        # The caller may veto persistence at exit — e.g. an immediately-exited
        # session that saved nothing would otherwise leave a freshly-created
        # container orphaned (kept "for resume" but no session references it).
        keep = backend is not None and _keep_sandbox_on_exit(persist, backend)
        if container:
            if keep:
                # Keep the container (and its writable layer + mount) so a later
                # session can reconnect. Stop it to free resources.
                try:
                    container.stop(timeout=10)
                    boot_status(f"sandbox: docker {container.id[:12]} stopped (kept for resume)")
                except Exception as e:  # noqa: BLE001
                    boot_status(f"sandbox: could not stop container: {e}", "warn")
            elif created_new:
                # Ephemeral: remove the freshly-created container on exit.
                console.print(f"[dim]Stopping Docker container {container.id[:12]}...[/dim]")
                try:
                    container.stop(timeout=10)
                    container.remove()
                    console.print(f"[dim]✓ Docker container {container.id[:12]} terminated[/dim]")
                except Exception as e:  # noqa: BLE001
                    console.print(f"[yellow]⚠ Cleanup failed: {e}[/yellow]")
            # else: reused & not persisted — leave running as-is.


_PROVIDER_TO_WORKING_DIR = {
    "modal": "/workspace",
    "runloop": "/home/user",
    "daytona": "/home/daytona",
    "docker": "/workspace",
    "harbor": "/app",
    "inmemory": "/workspace",
    "langsmith": "/home/user",
}


# Mapping of sandbox types to their context manager factories
_SANDBOX_PROVIDERS = {
    "modal": create_modal_sandbox,
    "runloop": create_runloop_sandbox,
    "daytona": create_daytona_sandbox,
    "docker": create_docker_sandbox,
    "langsmith": create_langsmith_sandbox,
}


class _Heartbeat:
    """Daemon thread that refreshes a sandbox's registry heartbeat.

    Proves the owning process is alive so the next startup can distinguish a
    crash-orphan (stale heartbeat) from a concurrent live session (fresh one).
    """

    def __init__(self, sandbox_id: str) -> None:
        from novacode_cli.integrations import sandbox_registry as reg

        self._id = sandbox_id
        self._interval = reg.HEARTBEAT_INTERVAL_SECS
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="nova-sandbox-heartbeat", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        from novacode_cli.integrations import sandbox_registry as reg

        while not self._stop.wait(self._interval):
            with suppress(Exception):
                reg.heartbeat(self._id)

    def stop(self) -> None:
        self._stop.set()


_EXIT_NET_INSTALLED = False


def _install_exit_net() -> None:
    """Install an atexit + SIGTERM net that terminates this process's sandboxes.

    The ``with create_sandbox`` ``finally`` already covers a normal exit and
    SIGINT (KeyboardInterrupt unwinds it). This adds a SIGTERM handler — ``kill``
    otherwise terminates without running cleanup — plus an atexit backstop. Both
    call the idempotent ``terminate_owned``. Installed once per process.
    """
    global _EXIT_NET_INSTALLED
    if _EXIT_NET_INSTALLED:
        return
    _EXIT_NET_INSTALLED = True

    from novacode_cli.integrations import sandbox_registry as reg

    atexit.register(reg.terminate_owned)

    def _on_sigterm(signum, frame):  # noqa: ANN001, ARG001
        with suppress(Exception):
            reg.terminate_owned()
        sys.exit(143)  # 128 + SIGTERM

    # Signal handlers can only be set from the main thread; never fatal if not.
    with suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGTERM, _on_sigterm)


@contextmanager
def create_sandbox(  # noqa: PLR0912
    provider: str,
    *,
    sandbox_id: str | None = None,
    setup_script_path: str | None = None,
    ports: dict[int, int] | None = None,
    mount_dir: str | None = None,
    persist: bool = False,
    session_id: str | None = None,
    vcpus: int | None = None,
    mem_bytes: int | None = None,
    fs_capacity_bytes: int | None = None,
    snapshot_id: str | None = None,
    snapshot_name: str | None = None,
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create or connect to a sandbox of the specified provider.

    This is the unified interface for sandbox creation that delegates to
    the appropriate provider-specific context manager.

    Args:
        provider: Sandbox provider ("modal", "runloop", "daytona", "docker", "langsmith")
        sandbox_id: Optional existing sandbox ID to reuse
        setup_script_path: Optional path to setup script to run after sandbox starts
        ports: Optional port mapping {container_port: host_port} (Docker/LangSmith)
        mount_dir: Optional host directory to bind-mount at /workspace (Docker only)
        persist: Keep the container on exit for later reconnect (Docker only)
        session_id: Session id used to name/label the container (Docker only)
        vcpus: Number of virtual CPUs (LangSmith only)
        mem_bytes: Memory in bytes (LangSmith only)
        fs_capacity_bytes: Filesystem capacity in bytes (LangSmith only)
        snapshot_id: Snapshot ID to boot from (LangSmith only)
        snapshot_name: Snapshot name to boot from (LangSmith only)

    Yields:
        (SandboxBackend, sandbox_id)
    """
    if provider not in _SANDBOX_PROVIDERS:
        msg = (
            f"Unknown sandbox provider: {provider}. "
            f"Available providers: {', '.join(get_available_sandbox_types())}"
        )
        raise ValueError(msg)

    from novacode_cli.integrations import sandbox_registry as reg

    # Startup reclaim: terminate any sandbox whose owning process is dead (a
    # crash-orphan), across all providers. Best-effort — never block startup.
    with suppress(Exception):
        reclaimed = reg.reclaim_dead_sandboxes()
        if reclaimed:
            console.print(
                f"[dim]Reclaimed {len(reclaimed)} orphaned sandbox(es) from a previous run.[/dim]"
            )

    sandbox_provider = _SANDBOX_PROVIDERS[provider]

    # Build kwargs for sandbox provider
    sandbox_kwargs: dict = {
        "sandbox_id": sandbox_id,
        "setup_script_path": setup_script_path,
    }

    # Docker-specific options
    if provider == "docker":
        if ports:
            sandbox_kwargs["ports"] = ports
        if mount_dir:
            sandbox_kwargs["mount_dir"] = mount_dir
        sandbox_kwargs["persist"] = persist
        if session_id:
            sandbox_kwargs["session_id"] = session_id

    # LangSmith-specific options
    if provider == "langsmith":
        if ports:
            sandbox_kwargs["ports"] = ports
        if vcpus is not None:
            sandbox_kwargs["vcpus"] = vcpus
        if mem_bytes is not None:
            sandbox_kwargs["mem_bytes"] = mem_bytes
        if fs_capacity_bytes is not None:
            sandbox_kwargs["fs_capacity_bytes"] = fs_capacity_bytes
        if snapshot_id is not None:
            sandbox_kwargs["snapshot_id"] = snapshot_id
        if snapshot_name is not None:
            sandbox_kwargs["snapshot_name"] = snapshot_name

    with sandbox_provider(**sandbox_kwargs) as backend:
        # Register ownership + start the liveness heartbeat so an abrupt end is
        # recoverable on the next launch. The InMemory test sandbox is skipped.
        heartbeat: _Heartbeat | None = None
        backend_id = getattr(backend, "id", None)
        if backend_id and provider != "inmemory":
            with suppress(Exception):
                reg.register(
                    backend_id,
                    provider=provider,
                    session_id=session_id,
                    persist=persist,
                )
                _install_exit_net()
                heartbeat = _Heartbeat(backend_id)
        try:
            yield backend
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            if backend_id and provider != "inmemory":
                with suppress(Exception):
                    reg.deregister(backend_id)


def get_available_sandbox_types() -> list[str]:
    """Get list of available sandbox provider types.

    Returns:
        List of sandbox type names (e.g., ["modal", "runloop", "daytona"])
    """
    return list(_SANDBOX_PROVIDERS.keys())


def get_default_working_dir(provider: str) -> str:
    """Get the default working directory for a given sandbox provider.

    Args:
        provider: Sandbox provider name ("modal", "runloop", "daytona")

    Returns:
        Default working directory path as string

    Raises:
        ValueError: If provider is unknown
    """
    if provider in _PROVIDER_TO_WORKING_DIR:
        return _PROVIDER_TO_WORKING_DIR[provider]
    msg = f"Unknown sandbox provider: {provider}"
    raise ValueError(msg)


class InMemorySandbox(BaseSandbox):
    """In-memory sandbox backend for testing.

    Implements ``SandboxBackendProtocol`` entirely in-process — no Docker
    daemon, Modal SDK, or network required. Stores uploaded files in a
    dict and executes commands as local subprocesses (when a shell is
    available) or returns canned responses.

    This is the test adapter that creates a *real seam*: provisioning tests,
    factory dispatch tests, and middleware tests can all run without any
    cloud SDK installed.

    Usage::

        from novacode_cli.integrations.sandbox_factory import (
            create_inmemory_sandbox,
        )

        with create_inmemory_sandbox() as backend:
            result = backend.execute("echo hello")
            assert result.exit_code == 0
    """

    def __init__(self) -> None:
        """Initialize an empty in-memory sandbox."""
        self._files: dict[str, bytes] = {}
        self.uploaded: list[tuple[str, bytes]] = []
        self.downloaded: list[str] = []

    @property
    def id(self) -> str:
        return "inmemory"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a command as a local subprocess.

        When a shell is available (not Windows without PowerShell), runs the
        command locally and captures output. Falls back to a canned response
        when there is no shell.
        """
        import subprocess  # noqa: S404

        try:
            result = subprocess.run(  # noqa: S602
                command,
                shell=True,
                capture_output=True,
                timeout=timeout or 30,
            )
            stdout = (result.stdout or b"").decode("utf-8", errors="replace")
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")
            output = stdout
            if stderr:
                output = output + "\n" + stderr if output else stderr
            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=False,
            )
        except FileNotFoundError:
            return ExecuteResponse(
                output="",
                exit_code=127,
                truncated=False,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output="<timed out>",
                exit_code=-1,
                truncated=False,
            )

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Async variant — delegates to :meth:`execute`."""
        return self.execute(command, timeout=timeout)

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download previously-uploaded files from the in-memory store."""
        self.downloaded.extend(paths)
        responses: list[FileDownloadResponse] = []
        for path in paths:
            content = self._files.get(path)
            if content is not None:
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            else:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=b"",
                        error=f"File not found: {path}",
                    )
                )
        return responses

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Async variant — delegates to :meth:`download_files`."""
        return self.download_files(paths)

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Store files in the in-memory dict."""
        self.uploaded.extend(files)
        responses: list[FileUploadResponse] = []
        for path, content in files:
            self._files[path] = content
            responses.append(FileUploadResponse(path=path, error=None, size=len(content)))
        return responses

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Async variant — delegates to :meth:`upload_files`."""
        return self.upload_files(files)


@contextmanager
def create_inmemory_sandbox(
    *,
    sandbox_id: str | None = None,
    setup_script_path: str | None = None,
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create an in-memory sandbox for testing.

    Args:
        sandbox_id: Ignored — included for protocol compatibility.
        setup_script_path: Ignored — included for protocol compatibility.

    Yields:
        An ``InMemorySandbox`` instance.
    """
    backend = InMemorySandbox()
    try:
        yield backend
    finally:
        pass  # Nothing to clean up


# Register in-memory provider
_SANDBOX_PROVIDERS["inmemory"] = create_inmemory_sandbox

__all__ = [
    "create_sandbox",
    "get_available_sandbox_types",
    "get_default_working_dir",
    "InMemorySandbox",
    "create_inmemory_sandbox",
]
