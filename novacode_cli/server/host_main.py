"""Entry point for the standalone TUI server (``nova --serve``).

Two callers, one implementation:

* ``nova --ui ratatui`` (via ``novacode_cli/ui/launcher.py``) binds a server in
  the same process that already built the agent, then spawns the client;
* ``nova --serve`` binds the same server and waits, so a frontend can attach from
  another process.

Both pass a session factory, so the agent bootstrap is never duplicated: the
server only decides *how* frames travel.

Logging goes to ``~/.nova/logs/tui-server.log``. A client owns the terminal, so a
stray ``print`` here would paint over the UI — stdout is never written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.server.host import SessionHost
from novacode_cli.server.transport import TuiServer

if TYPE_CHECKING:
    from novacode_cli.server.host import SessionSpec

logger = logging.getLogger(__name__)

LOG_NAME = "tui-server.log"


def log_path() -> Path:
    """Resolve the server log path.

    Returns:
        ``<NOVA_HOME>/logs/tui-server.log``, honouring an isolated ``NOVA_HOME``
        (tests and CI set it) and falling back to ``~/.nova``.
    """
    home = os.environ.get("NOVA_HOME") or str(Path.home() / ".nova")
    return Path(home) / "logs" / LOG_NAME


def configure_logging(path: Path | None = None) -> Path:
    """Send server diagnostics to a file, never to the terminal.

    Args:
        path: Log file to append to; defaults to :func:`log_path`.

    Returns:
        The path actually used. A file that cannot be opened falls back to the
        null handler so the server still starts.
    """
    target = path or log_path()
    handler: logging.Handler
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("novacode_cli.server")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False
    return target


async def serve_main(
    *,
    spec_factory: Any,  # noqa: ANN401 — a callable, kept loose like the transport's
    port: int = 0,
    token: str | None = None,
    host: str = "127.0.0.1",
    announce: bool = False,
) -> int:
    """Bind the server and serve until cancelled.

    Args:
        spec_factory: Builds the session to serve (see
            :class:`novacode_cli.server.host.SessionSpec`).
        port: Port to bind; ``0`` picks an ephemeral one.
        token: Shared secret; generated when omitted.
        host: Interface to bind. Loopback by default and by policy.
        announce: Write the port and token to stderr when the server is meant to
            be attached to by hand (``nova --serve``). Never used under the
            launcher, where the client gets them on its command line.

    Returns:
        Process exit code: ``0`` on a clean shutdown, ``130`` on interrupt.
    """
    configure_logging()

    async def host_factory(emit: Any) -> SessionHost:  # noqa: ANN401 — transport sink
        return SessionHost(emit, spec_factory)

    server = TuiServer(host_factory, token=token, host=host, port=port)
    bound = await server.start()
    if announce:
        sys.stderr.write(f"nova-tui server listening on {host}:{bound} token={server.token}\n")
        sys.stderr.flush()
    logger.info("serving %s:%s", host, bound)

    stop = asyncio.Event()
    try:
        await stop.wait()
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130
    finally:
        with contextlib.suppress(Exception):
            await server.stop()
    return 0


async def serve_once(spec: SessionSpec, *, port: int = 0, token: str | None = None) -> int:
    """Serve exactly one pre-built session until interrupted.

    Args:
        spec: The already-built session (the ``nova --serve`` path, after the
            normal bootstrap).
        port: Port to bind.
        token: Shared secret; generated when omitted.

    Returns:
        Process exit code.
    """
    return await serve_main(
        spec_factory=lambda: spec, port=port, token=token, announce=True
    )
