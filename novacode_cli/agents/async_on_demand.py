"""Start Nova's own agent server when an async agent is first dispatched.

The server used to be started while Nova booted, whether or not the session ever
delegated anything: a second Python process holding six compiled graphs, on a
machine that may have no memory to spare. It is now only *planned* at boot (a
port is reserved, so the specs can name it) and started here, by the first
``start_async_task``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

#: The dispatch tool. The others (check/update/cancel/list) only make sense once
#: something has been started, so they never trigger a launch.
START_TOOL = "start_async_task"

_FAILED = (
    "The async agent server could not be started, so no background task was "
    "launched. Delegate this with the `task` tool (an in-process subagent) instead."
)


class AsyncServerOnDemandMiddleware(AgentMiddleware):
    """Bring the planned agent server up before the first async dispatch."""

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:  # noqa: ANN401
        call = getattr(request, "tool_call", None) or {}
        if call.get("name") == START_TOOL:
            from novacode_cli.agents import server_launcher

            if not await server_launcher.ensure_started():
                return ToolMessage(content=_FAILED, tool_call_id=call.get("id", ""), status="error")
        return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:  # noqa: ANN401
        """Sync twin, for a subagent invoked with ``invoke``.

        That runs on a worker thread with no event loop of its own, so the
        launch gets a short-lived one.
        """
        call = getattr(request, "tool_call", None) or {}
        if call.get("name") == START_TOOL:
            from novacode_cli.agents import server_launcher

            if not asyncio.run(server_launcher.ensure_started()):
                return ToolMessage(content=_FAILED, tool_call_id=call.get("id", ""), status="error")
        return handler(request)
