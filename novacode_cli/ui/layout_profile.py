"""Where each workspace's UI layout profile lives.

One file per workspace, keyed by a hash of its resolved path, under
``~/.nova/ui``. It sits outside the TUI package because the socket server has to
name the same file when it explains what `/ui` would edit, and that package
eagerly imports Textual (ADR-003).
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def profile_path(workspace: Path) -> Path:
    """Return the layout profile for *workspace*.

    Args:
        workspace: The directory the layout belongs to.

    Returns:
        ``~/.nova/ui/<16 hex chars>.json``.
    """
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
    return Path.home() / ".nova" / "ui" / f"{key}.json"
