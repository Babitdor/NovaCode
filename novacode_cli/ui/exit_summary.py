"""A compact Nova lockup and a verified session continuation hint after the TUI."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from rich.console import Console
from rich.text import Text

from novacode_cli._version import __version__
from novacode_cli.brand import WORDMARK_WIDTH, compact_mark, wordmark


def build_exit_message(session_id: str, name: str, *, saved: bool, width: int) -> Text:
    """Use literal styled text so session names cannot become terminal markup."""
    name = " ".join(
        "".join(
            char for char in str(name or session_id) if char.isprintable() or char.isspace()
        ).split()
    )[:100]
    logo = wordmark() if width >= WORDMARK_WIDTH else compact_mark()
    text = Text("\n")
    text.append(logo, style="bold bright_black")
    text.append(f"\nNovaCode v{__version__}\n\n", style="dim")
    text.append("Session  ", style="dim")
    text.append(name if saved else "No saved conversation", style="bold")
    if saved:
        command = (
            f"nova --continue {session_id}"
            if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id)
            else "nova --resume"
        )
        text.append("\nContinue ", style="dim")
        text.append(command, style="bold")
    text.append("\n")
    return text


async def print_exit_summary(app: Any, *, console: Console | None = None) -> None:
    """Read only root metadata; failed or empty saves never get a resume command."""
    if console is None:
        from novacode_cli.config.config import console as terminal

        console = terminal
    root = getattr(app, "_root_pane", None)
    state = getattr(app, "session_state", None)
    if root is not None and root is not getattr(app, "_active_pane", None):
        state = root.state.get("session_state", state)
    sid = str(getattr(state, "session_id", "") or "")
    name = getattr(root, "title", "") or "main"
    saved = False
    manager = getattr(app, "session_manager", None)
    if sid and manager is not None:
        try:
            meta = await asyncio.wait_for(
                asyncio.to_thread(manager.get_session_meta, sid), timeout=2
            )
            saved = bool(meta and meta.message_count and not meta.cleared)
            if saved:
                name = meta.current_task or name
        except Exception:
            pass  # Exit remains usable when the persistence directory is unavailable.
    console.print(build_exit_message(sid, name, saved=saved, width=console.width))
