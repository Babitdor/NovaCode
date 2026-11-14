"""Immediate workspace/assistant-scoped knowledge cache controls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.config.config import console
from novacode_cli.tools.knowledge_tools import KnowledgeLookup

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry
    from novacode_cli.states.Session import SessionState


async def control_cache(
    session_state: SessionState, assistant_id: str, action: str, sandbox_type: str | None = None
) -> dict:
    """Share the live workspace-scoped cache operation across CLI and TUI."""
    backend = getattr(session_state, "_backend", None)
    default = getattr(backend, "default", None)
    root = getattr(default, "cwd", None)
    if sandbox_type or root is None:
        return {"error": "Knowledge cache is available in local main-agent sessions."}
    lookup = KnowledgeLookup(Path(root), assistant_id, backend, None)
    return await lookup.control(action)


async def handle_cache_command(ctx: CommandContext) -> bool:
    """Keep structured console output for headless callers."""
    result = await control_cache(
        ctx.session_state,
        ctx.assistant_id,
        (ctx.cmd_args or "status").strip().lower(),
        ctx.sandbox_type,
    )
    console.print(json.dumps(result, indent=2), markup=False)
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Register the cache controls for TUI and headless dispatch."""
    registry.register("cache", handle_cache_command)
