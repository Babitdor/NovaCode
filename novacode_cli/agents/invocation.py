"""Resolve persistent agent invocations and refresh rosters between turns."""

# ruff: noqa: INP001

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from novacode_cli.config.config import Settings

logger = logging.getLogger(__name__)

DELEGATION_PREFIX = (
    "Delegate this request to the named specialist agents using these tool routes:\n"
)
_FIRST_ROUTE = re.compile(r"- @\S+: `(task|start_async_task)\(")


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
        DELEGATION_PREFIX
        + "\n".join(routes)
        + "\nFollow the requested order and pass verified results to dependent steps. "
        "A background task ID is not a completed result; obtain its result before a "
        "dependent step. Keep pending work tracked and avoid repeated status polling."
        + f"\n\nRequest:\n{text}"
    )


def forced_tool(request: ModelRequest) -> str | None:
    """The first route's tool when this is the first model call of an @agent turn."""
    last = request.messages[-1] if request.messages else None
    if not isinstance(last, HumanMessage):
        return None
    text = last.text.lstrip()
    if not text.startswith(DELEGATION_PREFIX):
        return None
    match = _FIRST_ROUTE.match(text[len(DELEGATION_PREFIX) :])
    if match is None:
        return None
    name = match.group(1)
    bound = {t.get("name") if isinstance(t, dict) else t.name for t in request.tools}
    return name if name in bound else None


class DelegationMiddleware(AgentMiddleware):
    """Make an @agent turn start by dispatching the subagent, not by thinking about it.

    The first model call of the turn must call the first route's tool. Providers
    that reject a forced tool_choice (Gemini VALIDATED mode, Anthropic extended
    thinking) get the same call retried unforced, which is the prompt-only
    behaviour; providers that ignore tool_choice behave as before.
    """

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """Force the first dispatch for a synchronous model call."""
        tool = forced_tool(request)
        if tool is None:
            return handler(request)
        try:
            return handler(request.override(tool_choice=tool))
        except Exception:  # noqa: BLE001 -- any rejection falls back to the unforced call.
            logger.info("Forced %s rejected; retrying unforced", tool, exc_info=True)
            return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Force the first dispatch for an async model call."""
        tool = forced_tool(request)
        if tool is None:
            return await handler(request)
        try:
            return await handler(request.override(tool_choice=tool))
        except Exception:  # noqa: BLE001 -- any rejection falls back to the unforced call.
            logger.info("Forced %s rejected; retrying unforced", tool, exc_info=True)
            return await handler(request)
