"""Choose the workspace for a resumed conversation without granting folder trust."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from novacode_cli.session.session_persistence import SessionManager, SessionMeta


async def choose_resume_directory(
    meta: SessionMeta | None,
    current: Path,
    picker: Callable[[Path, Path], Awaitable[int | None]],
) -> Path | None:
    """Use an explicit directory choice or saved preference; cancellation changes nothing."""
    saved = getattr(meta, "project_root", None)
    current = await asyncio.to_thread(current.resolve)
    if not saved:
        return current
    session = await asyncio.to_thread(lambda: Path(saved).expanduser().resolve())
    if session == current:
        return current

    from novacode_cli.config.nova_config import NovaConfig

    config = await asyncio.to_thread(NovaConfig)
    preference = config.get("resume_directory", "ask")
    choice = {"session": 0, "current": 1}.get(preference) if isinstance(preference, str) else None
    if choice is None:
        choice = await picker(session, current)
    if choice not in (0, 1, 2, 3):
        return None
    target = session if choice in (0, 2) else current
    target = await asyncio.to_thread(target.resolve, strict=True)
    if not await asyncio.to_thread(target.is_dir):
        message = f"Resume directory is not a folder: {target}"
        raise ValueError(message)
    if choice in (2, 3):
        await asyncio.to_thread(
            config.set,
            "resume_directory",
            "session" if choice == 2 else "current",  # noqa: PLR2004 - Screen choice index.
        )
    return target


async def prepare_cli_resume(
    manager: SessionManager,
    current: Path,
    *,
    resume: bool,
    session_id: str | None,
    compact: bool = False,
) -> tuple[str, bool, Path] | None:
    """Resolve the interactive startup choices before allocating an agent or sandbox."""
    from novacode_cli.session.session_restore import select_session_interactive
    from novacode_cli.tui.pickers import pick_resume_directory_tui

    if resume:
        picked = await select_session_interactive(manager)
        if picked is None:
            return None
        session_id, compact = picked
    if not session_id:
        return None
    meta = await asyncio.to_thread(manager.load_session_meta, session_id)
    directory = await choose_resume_directory(meta, current, pick_resume_directory_tui)
    return (session_id, compact, directory) if directory is not None else None


def resume_argv(arguments: Sequence[str], session_id: str, *, compact: bool) -> list[str]:
    """Keep CLI options while replacing the completed interactive resume questions."""
    result = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in {"--resume", "-r", "--compact-on-resume"}:
            index += 1
            continue
        if value in {"--continue", "-c"}:
            index += 1
            if index < len(arguments) and not arguments[index].startswith("-"):
                index += 1
            continue
        if not value.startswith("--continue="):
            result.append(value)
        index += 1
    result.extend(["--continue", session_id])
    if compact:
        result.append("--compact-on-resume")
    return result


def relaunch_resume(directory: Path, arguments: Sequence[str]) -> int:
    """Start in the chosen directory so every imported tool binds to that workspace."""
    process = subprocess.Popen(  # noqa: S603 - Explicit argv, same interpreter, no shell.
        [sys.executable, "-m", "novacode_cli", *arguments], cwd=directory
    )
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:  # Ctrl+C reaches both; let the child save and shut down.
            continue
