"""Resolve saved tab identities without accepting filesystem paths as IDs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novacode_cli.session.session_persistence import SessionManager, SessionMeta


def resolve_saved_tab(manager: SessionManager, selector: str) -> SessionMeta:
    """Accept an exact ID or unique prefix, retaining the saved project binding."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", selector):
        message = "Use a saved session ID or unique ID prefix."
        raise ValueError(message)
    meta = manager.load_session_meta(selector)
    if meta is None:
        matches = [
            item
            for item in manager.list_sessions(limit=10000, include_cleared=True)
            if item.session_id.startswith(selector)
        ]
        if len(matches) != 1:
            message = "Session ID is ambiguous." if matches else "Saved session not found."
            raise ValueError(message)
        meta = matches[0]
    if not meta.project_root:
        message = "This session has no saved project folder."
        raise ValueError(message)
    return meta
