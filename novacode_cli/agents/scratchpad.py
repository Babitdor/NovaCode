"""Keep the scratch mount and prompt bound to the live conversation."""
# ruff: noqa: INP001

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.sessions import scratchpad

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends import FilesystemBackend
    from langchain.agents.middleware.types import ModelRequest, ModelResponse


class ScratchpadMiddleware(AgentMiddleware):
    """Refresh the mount after /clear without rebuilding the compiled graph."""

    def __init__(self, backend: FilesystemBackend, session_id: str) -> None:
        """Use the existing scratch mount for this graph."""
        super().__init__()
        self._backend = backend
        self._session_id = session_id

    def _request(self, request: ModelRequest) -> ModelRequest:
        session_id = getattr(active_plan_session.get(), "session_id", None) or self._session_id
        info = scratchpad.provision(session_id)
        if info is None:
            return request
        self._backend.cwd = info.host_path
        if self._session_id != session_id:
            scratchpad.cleanup(self._session_id)
            self._session_id = session_id
        note = (
            "\n<scratchpad>Session scratchpad (virtual): /scratchpad/\n"
            f"Session scratchpad (host path): {info.host_path}\n"
            "Use this directory for throwaway scripts, intermediate outputs, drafts and "
            "helper files. File read/write/edit operations here follow session policy; "
            "explicit denies still apply. Shell execution requires normal approval. "
            "Use virtual paths with file tools and the host path with shell commands. "
            "Copy final artifacts into the project to keep them; scratch files are "
            "removed when this session ends.</scratchpad>"
        )
        content = request.system_message.content if request.system_message else ""
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        return request.override(
            system_message=SystemMessage(content=[*blocks, {"type": "text", "text": note}])
        )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """Refresh the scratch context before a synchronous model call."""
        return handler(self._request(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """Refresh the scratch context before an asynchronous model call."""
        return await handler(self._request(request))
