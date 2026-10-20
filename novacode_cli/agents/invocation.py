"""Resolve persistent agent invocations and refresh rosters between turns."""

# ruff: noqa: INP001

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from novacode_cli.config.config import Settings


def definition_signature() -> str:
    """Hash definitions and opt-in team gates for safe next-turn rebuilding."""
    from novacode_cli.config.config import agent_md, settings
    from novacode_cli.config.nova_config import NovaConfig

    digest = hashlib.sha256()
    digest.update(str(NovaConfig().get_agent_team_settings()).encode())
    for name, path, scope in settings.get_all_agents():
        digest.update(str((name, str(path), scope)).encode())
        try:
            digest.update(agent_md(path).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


async def refresh_definitions(agent: Any, backend: Any, state: Any) -> tuple[Any, Any]:  # noqa: ANN401 - Existing runtime boundary.
    """Refresh a root's compiled roster at a serialized turn boundary.

    Explicitly bound teammates and graphs without a stored runtime context are
    left alone. Rebuild errors propagate so a turn cannot silently use stale
    tools after a permission or definition change.
    """
    previous = getattr(agent, "agent_definitions_signature", None)
    if previous is None or getattr(state, "_model", None) is None:
        return agent, backend
    current = await asyncio.to_thread(definition_signature)
    if current == previous:
        return agent, backend
    return await state.reload_agent_definitions()


def delegation_prompt(text: str, names: list[str], settings: Settings) -> str:
    """Choose the proper tool for each mention without converting async-only to sync."""
    from novacode_cli.agents.default_subagents.async_subagents import async_agents_usable
    from novacode_cli.agents.user_async_agents import (
        graph_id_for,
        is_background_only,
        read_agent_safe,
    )
    from novacode_cli.config.config import agent_md

    routes = []
    for name in names:
        found = settings.find_agent(name)
        background = bool(found and is_background_only(read_agent_safe(agent_md(found[0]))[0]))
        if background and not async_agents_usable():
            message = (
                f"@{name} is background-only, but no agent server is available for this "
                "workspace. Start/configure /agent-server, then retry."
            )
            raise ValueError(message)
        tool = "start_async_task" if background else "task"
        target = graph_id_for(name) if background else name
        routes.append(f"- @{name}: `{tool}(subagent_type={target!r}, description=...)`.")
    return (
        "Delegate this request to the named specialist agents using these tool routes:\n"
        + "\n".join(routes)
        + "\nFollow the requested order and pass verified results to dependent steps. "
        "A background task ID is not a completed result; obtain its result before a "
        "dependent step. Keep pending work tracked and avoid repeated status polling."
        + f"\n\nRequest:\n{text}"
    )
