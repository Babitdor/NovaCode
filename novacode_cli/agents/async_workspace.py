"""The directory the async-agent graphs work in.

The graphs used ``Path.cwd()`` of the server process, which is wherever
``langgraph.json`` lives: Nova's own repo. Launched from another project they
scouted Nova's source and reported it as findings about that project.

The launcher now names the session's workspace in ``NOVA_WORKSPACE_ROOT`` and the
graphs read it here. Without the variable (the Docker container, or a server
someone started by hand) the server's own directory is used, as before.

Resolved once, at import: ``os.getcwd()`` is a blocking call that ``langgraph
dev`` rejects on its event loop.
"""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_ROOT_VAR = "NOVA_WORKSPACE_ROOT"

_ROOT = Path(os.environ.get(WORKSPACE_ROOT_VAR, "").strip() or os.getcwd()).resolve()


def workspace_root() -> Path:
    """Where the graphs read, search and run commands."""
    return _ROOT
