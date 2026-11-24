"""Launch the ratatui client against a server already bound in this process.

The Python process stays the owner of the session; the Rust binary is handed the
TCP endpoint and the token and then owns the terminal. That ordering matters:
the socket is listening *before* the client is spawned, so the client's connect
never races the listener, and the client's exit is what ends the session.

Failure policy (ADR-004): a missing binary is not an error the user should pay
for. One actionable line is printed and ``False`` is returned, so the caller can
fall back to the Textual TUI and the session still starts.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from novacode_cli.server.host import SessionSpec

logger = logging.getLogger(__name__)

BINARY_NAME = "nova-tui"
BINARY_ENV = "NOVA_TUI_BIN"

# Mirrors sessions/supervisor.py: main.py ends in os._exit(), which runs no
# finally blocks, so a surviving child needs an atexit sweep to be reaped.
_live: set[Any] = set()


def binary_filename() -> str:
    """Return the platform's binary name.

    Returns:
        ``nova-tui.exe`` on Windows, ``nova-tui`` elsewhere.
    """
    return f"{BINARY_NAME}.exe" if sys.platform == "win32" else BINARY_NAME


def candidate_paths() -> list[Path]:
    """List where the client binary is looked for, in priority order.

    Returns:
        Candidate paths: ``NOVA_TUI_BIN``, the package's ``bin/`` directory, the
        user's ``~/.nova/bin``, then ``PATH`.
    """
    name = binary_filename()
    candidates: list[Path] = []
    override = os.environ.get(BINARY_ENV)
    if override:
        candidates.append(Path(override).expanduser())
    with contextlib.suppress(ImportError, AttributeError):
        import novacode_cli

        candidates.append(Path(novacode_cli.__file__).resolve().parent / "bin" / name)
    home = os.environ.get("NOVA_HOME") or str(Path.home() / ".nova")
    candidates.append(Path(home) / "bin" / name)
    found = shutil.which(BINARY_NAME)
    if found:
        candidates.append(Path(found))
    return candidates


def resolve_binary() -> Path | None:
    """Find the ratatui client binary.

    Returns:
        The first existing candidate, or ``None` when the client is not installed.
    """
    for candidate in candidate_paths():
        if candidate.is_file():
            return candidate
    return None


def _sweep_live_children() -> None:
    """atexit: kill a client that outlived us (os._exit skips finally blocks)."""
    from novacode_cli.shell.jobs import _kill_pid_tree

    for proc in list(_live):
        if getattr(proc, "returncode", None) is None:
            with contextlib.suppress(Exception):
                _kill_pid_tree(proc.pid)


def _track(proc: Any) -> None:  # noqa: ANN401 — asyncio.subprocess.Process
    """Remember a live client so the atexit sweep can reach it.

    Args:
        proc: The spawned client process.
    """
    _live.add(proc)


# Registered at import so a client that outlives an `os._exit()` still gets
# reaped. Suppressed because importing during interpreter shutdown cannot
# register a handler, and that must not turn an import into a crash.
with contextlib.suppress(RuntimeError):
    atexit.register(_sweep_live_children)


def _untrack(proc: Any) -> None:  # noqa: ANN401 — asyncio.subprocess.Process
    """Forget a client that has exited.

    Args:
        proc: The client process.
    """
    _live.discard(proc)


async def launch_ratatui(spec: SessionSpec) -> bool:
    """Serve *spec* on a loopback socket and run the ratatui client against it.

    Args:
        spec: The already-built session to serve.

    Returns:
        ``True`` when the client ran (whatever its exit code, which becomes the
        process's), ``False`` when the binary is missing and the caller should
        fall back to Textual.
    """
    from novacode_cli.config.config import console
    from novacode_cli.server.host import SessionHost
    from novacode_cli.server.transport import TuiServer

    binary = resolve_binary()
    if binary is None:
        console.print(
            "[yellow]nova-tui (the Rust client) is not installed.[/yellow] "
            "[dim]Download the matching binary from https://github.com/Babitdor/NovaCode/releases "
            "and set NOVA_TUI_BIN to its path. "
            "Starting the Textual UI instead.[/dim]"
        )
        return False

    # The transport awaits its factory — a host is only built once the token
    # checks out — so this has to be a coroutine function, not a plain lambda.
    async def host_factory(emit: Any) -> SessionHost:  # noqa: ANN401 — transport sink
        return SessionHost(emit, lambda: spec)

    server = TuiServer(host_factory)
    port = await server.start()
    proc: Any = None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            server.token,
        )
        _track(proc)
        code = await proc.wait()
    except OSError as exc:
        logger.warning("could not start %s: %s", binary, exc)
        console.print(f"[red]Could not start {binary}: {exc}[/red]")
        return False
    else:
        # The client owned the terminal; its exit code becomes ours.
        logger.info("nova-tui exited with %s", code)
        state = getattr(spec, "session_state", None)
        if state is not None:
            state.headless_exit_code = code
        return True
    finally:
        if proc is not None:
            _untrack(proc)
            if getattr(proc, "returncode", None) is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        # Release the port even if the client died on its first frame.
        with contextlib.suppress(Exception):
            await server.stop()
