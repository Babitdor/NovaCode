"""Bind memory wiki maintenance to one agent's own persistent directory."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from langchain.tools import tool

from novacode_cli.memory.wiki import maintain_wiki, validate_wiki

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.tools import BaseTool


def create_memory_wiki_tool(agent_dir: Path) -> BaseTool:
    """Create a tool with its destination bound by the application, not the model."""

    @tool
    def memory_wiki(action: Literal["check", "maintain"] = "check") -> str:
        """Check or rebuild your linked memory wiki after consolidating topic notes.

        Check reports broken relative links. Maintain backs up source files,
        rebuilds the compact INDEX and topic maps, and refreshes related links.
        It preserves topic bodies and user rules; it does not decide which
        conflicting claims are true or delete unique facts. Use after /dream.
        """
        try:
            result = (
                maintain_wiki(agent_dir)
                if action == "maintain"
                else {"broken_links": validate_wiki(agent_dir)}
            )
            return json.dumps(result)
        except (OSError, ValueError, RuntimeError) as exc:
            return json.dumps({"error": str(exc), "complete": False})

    return memory_wiki
