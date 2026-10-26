"""Immediate workspace/assistant-scoped knowledge cache controls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.config.config import console
from novacode_cli.tools.knowledge_tools import KnowledgeLookup

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry


async def handle_cache_command(ctx: CommandContext) -> bool:
    """Use the live backend root, including session worktree transitions."""
    backend = getattr(ctx.session_state, "_backend", None)
    default = getattr(backend, "default", None)
    root = getattr(default, "cwd", None)
    if ctx.sandbox_type or root is None:
        console.print("Knowledge cache is available in local main-agent sessions.")
        return True
    lookup = KnowledgeLookup(Path(root), ctx.assistant_id, backend, None)
    result = await lookup.control((ctx.cmd_args or "status").strip().lower())
    console.print(json.dumps(result, indent=2), markup=False)
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Register the cache controls for TUI and headless dispatch."""
    registry.register("cache", handle_cache_command)
