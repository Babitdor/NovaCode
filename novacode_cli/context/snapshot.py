"""Immutable assembled requests and content-free accounting diagnostics."""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

from novacode_cli.context.estimates import estimate_content
from novacode_cli.context.observations import compact_observations

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware


class ProtectedContextOverflowError(RuntimeError):
    """Standing instructions and user constraints cannot fit safely."""


@dataclass(frozen=True)
class ContextSnapshot:
    """Serialized content cannot be mutated by concurrent prompt assembly."""

    session: str
    task: str
    generation: str
    provider: str
    model: str
    messages: tuple[str, ...]
    schemas: tuple[str, ...]
    protected: tuple[str, ...]
    recovery: tuple[str, ...]
    estimated_input_tokens: int
    protected_tokens: int
    output_reservation: int
    confidence: str = "approximate"
    provider_prepared: tuple[str, ...] = ()
    estimated_text_tokens: int = 0
    estimated_schema_tokens: int = 0
    estimated_multimodal_tokens: int = 0
    image_transport_chars: int = 0
    estimated_system_tokens: int = 0
    #: MCP tool schemas sent with this request, and their size.
    mcp_tools: int = 0
    mcp_schema_tokens: int = 0
    #: The MCP server/tool list injected into the system prompt.
    mcp_prompt_tokens: int = 0

    @classmethod
    def capture(cls, request: ModelRequest) -> ContextSnapshot:
        """Freeze the complete assembled Nova request before provider dispatch."""
        from langchain_core.messages import AIMessage, ToolMessage

        from novacode_cli.agents.context_compaction import pending_tool_start, user_messages
        from novacode_cli.tracking.request_metrics import task_correlation

        messages = ([request.system_message] if request.system_message else []) + list(
            request.messages
        )
        encoded = tuple(m.model_dump_json() for m in messages)
        schemas = tuple(
            json.dumps(convert_to_openai_tool(t), sort_keys=True, default=str)
            for t in request.tools or []
        )
        protected = tuple(user_messages(request.messages, 0))
        pending = [
            m
            for m in request.messages[pending_tool_start(request.messages) :]
            if isinstance(m, (AIMessage, ToolMessage))
        ]
        pending_tokens = estimate_content(pending).total
        protected += tuple(m.model_dump_json() for m in pending)
        system_tokens = (
            estimate_content([request.system_message]).total if request.system_message else 0
        )
        # Use the same estimator for both pressure and reporting.
        tool_tokens = (
            count_tokens_approximately([], tools=list(request.tools)) if request.tools else 0
        )
        mcp_tools = [
            t
            for t in request.tools or []
            if (getattr(t, "metadata", None) or {}).get("nova_mcp_server")
        ]
        mcp_schema_tokens = count_tokens_approximately([], tools=mcp_tools) if mcp_tools else 0
        mcp_prompt_tokens = _mcp_prompt_tokens(request.system_message)
        settings = getattr(request, "model_settings", None) or {}
        reserve = settings.get("max_tokens") or getattr(request.model, "max_tokens", None) or 0
        from collections.abc import Mapping

        raw_event = (request.state or {}).get("_summarization_event")
        event = raw_event if isinstance(raw_event, Mapping) else {}
        summary = event.get("summary_message")
        recovery = (
            tuple(summary.additional_kwargs.get("history_files", [])) if summary is not None else ()
        )
        from langgraph.config import get_config

        try:
            identity = str(get_config().get("configurable", {}).get("thread_id", ""))
        except RuntimeError:
            identity = ""
        source_identity = (
            "".join(encoded) + "".join(schemas) + json.dumps(event, sort_keys=True, default=str)
        )
        from novacode_cli.context.provider_accounting import prepared_request

        prepared, effective_reservation = prepared_request(request, messages, schemas)
        structured_tokens = 0
        if prepared:
            payload = json.loads(prepared[0])
            # Provider preparation adds structured-output schemas after Nova's
            # tool list is assembled. Include those in the shared pressure and
            # reporting estimate; provider usage remains separately reported.
            response_schema = payload.get("response_format")
            extra_tools = payload.get("tools", [])[len(schemas) :]
            if response_schema:
                structured_tokens += max(1, len(json.dumps(response_schema)) // 4)
            if extra_tools:
                structured_tokens += max(1, len(json.dumps(extra_tools)) // 4)
            source_identity += "".join(prepared)
        if effective_reservation is not None:
            reserve = effective_reservation
        content_estimate = estimate_content(messages)
        return cls(
            identity,
            task_correlation.get() or "",
            hashlib.sha256(source_identity.encode()).hexdigest(),
            type(request.model).__module__,
            str(
                getattr(request.model, "model_name", None)
                or getattr(request.model, "model", "unknown")
            ),
            encoded,
            schemas,
            protected,
            recovery,
            content_estimate.total + tool_tokens + structured_tokens,
            system_tokens
            + tool_tokens
            + structured_tokens
            + pending_tokens
            + sum(max(1, len(p) // 3) for p in user_messages(request.messages, 0)),
            int(reserve),
            "provider_prepared_estimate" if prepared else "approximate",
            prepared,
            content_estimate.text_tokens,
            tool_tokens + structured_tokens,
            content_estimate.image_tokens,
            content_estimate.image_transport_chars,
            system_tokens,
            len(mcp_tools),
            mcp_schema_tokens,
            mcp_prompt_tokens,
        )

    def diagnostics(self) -> dict[str, object]:
        """Return estimates and identifiers, without message or schema content."""
        from novacode_cli.tracking.request_metrics import request_purpose

        return {
            "generation": self.generation,
            "model": self.model,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_text_tokens": self.estimated_text_tokens,
            "estimated_schema_tokens": self.estimated_schema_tokens,
            "estimated_multimodal_tokens": self.estimated_multimodal_tokens,
            "multimodal_estimate_method": "fixed_85_per_image; dimensions_unobserved",
            "image_transport_chars": self.image_transport_chars,
            "estimated_system_tokens": self.estimated_system_tokens,
            "mcp_tools": self.mcp_tools,
            "mcp_schema_tokens": self.mcp_schema_tokens,
            "mcp_prompt_tokens": self.mcp_prompt_tokens,
            "purpose": request_purpose.get(),
            "retry_usage": "unavailable",
            "protected_tokens": self.protected_tokens,
            "output_reservation": self.output_reservation,
            "confidence": self.confidence,
            "message_count": len(self.messages),
            "schema_count": len(self.schemas),
            "recovery": list(self.recovery),
            "provider_prepared_bytes": sum(len(p.encode()) for p in self.provider_prepared)
            if self.provider_prepared
            else None,
        }

    def validate(self, window: int) -> None:
        """Fail closed when preserved constraints exceed usable capacity."""
        if self.protected_tokens + self.output_reservation > window:
            message = (
                "Protected instructions and tool schemas exceed the usable context window. "
                "Select a model with a larger window or explicitly revise "
                "the task constraints; history has been retained."
            )
            raise ProtectedContextOverflowError(message)


def _remembered_path() -> Path:
    from pathlib import Path

    from novacode_cli.config.config import HOME_DIR

    return Path(HOME_DIR) / "context_baseline.json"


def remember_system_tokens(agent: str, tokens: int) -> None:
    """Keep the last measured system prompt size, for /context before a request.

    The prompt sent is the core prompt plus what middleware injects per request
    (MCP servers, skills, memory): over twice the startup estimate, and nearly
    the same from one session to the next.
    """
    path = _remembered_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get(agent) == tokens:
        return
    data[agent] = tokens
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(data), encoding="utf-8")


def remembered_system_tokens(agent: str) -> int:
    """The last measured system prompt size for ``agent``, or 0."""
    try:
        value = json.loads(_remembered_path().read_text(encoding="utf-8")).get(agent)
    except (OSError, ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) else 0


#: The heading the MCP middleware's prompt section starts with (prompts/mcp.jinja).
_MCP_HEADING = "## MCP Tools"


def _mcp_prompt_tokens(system_message: object) -> int:
    """Size of the MCP section of the system prompt, up to the next ``## `` heading."""
    if system_message is None:
        return 0
    content = getattr(system_message, "content", "")
    if not isinstance(content, str):
        content = "\n".join(
            str(b.get("text", "")) for b in content or [] if isinstance(b, dict)
        )
    start = content.find(_MCP_HEADING)
    if start < 0:
        return 0
    end = content.find("\n## ", start + len(_MCP_HEADING))
    section = content[start : end if end >= 0 else len(content)]
    return max(1, len(section) // 4)


def _replace_snapshot(
    _previous: dict[str, object], current: dict[str, object]
) -> dict[str, object]:
    """Keep one complete request diagnostic when parallel nodes update state.

    Accounting describes a single request, so merging fields from different
    requests would produce an invalid snapshot. LangGraph applies updates in
    graph order; the last diagnostic replaces the previous one.
    """
    return current


class SnapshotState(AgentState):
    """Persist only content-free request diagnostics."""

    _context_snapshot: NotRequired[Annotated[dict[str, object], _replace_snapshot]]


class ContextSnapshotMiddleware(AgentMiddleware[SnapshotState]):
    """Last Nova middleware: observe the request after prompt assembly."""

    state_schema = SnapshotState

    def _capture(self, request: ModelRequest) -> ContextSnapshot:
        from novacode_cli.compaction import _window_tokens

        snapshot = ContextSnapshot.capture(request)
        snapshot.validate(_window_tokens(request.model))
        return snapshot

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ExtendedModelResponse:
        """Capture the asynchronous dispatch and its observed usage."""
        request = request.override(messages=compact_observations(request.messages))
        snapshot = self._capture(request)
        from novacode_cli.context.provider_accounting import (
            AttemptCounter,
            http_attempts,
            instrument_http,
        )

        counter = AttemptCounter(observed=instrument_http(request.model))
        token = http_attempts.set(counter)
        try:
            response = await handler(request)
            return self._result(response, snapshot, counter.count if counter.observed else None)
        finally:
            http_attempts.reset(token)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ExtendedModelResponse:
        """Capture the synchronous dispatch and its observed usage."""
        request = request.override(messages=compact_observations(request.messages))
        snapshot = self._capture(request)
        return self._result(handler(request), snapshot)

    @staticmethod
    def _result(
        response: ModelResponse | ExtendedModelResponse,
        snapshot: ContextSnapshot,
        attempts: int | None = None,
    ) -> ExtendedModelResponse:
        diagnostic = snapshot.diagnostics()
        diagnostic["http_attempts"] = attempts
        replies = getattr(response, "result", [])
        diagnostic["provider_input_tokens"] = next(
            (
                m.usage_metadata.get("input_tokens")
                for m in reversed(replies)
                if getattr(m, "usage_metadata", None)
            ),
            None,
        )
        if isinstance(response, ExtendedModelResponse):
            update = dict(response.command.update or {}) if response.command else {}
            update["_context_snapshot"] = diagnostic
            return ExtendedModelResponse(
                model_response=response.model_response, command=Command(update=update)
            )
        return ExtendedModelResponse(
            model_response=response, command=Command(update={"_context_snapshot": diagnostic})
        )


def background_context_middleware() -> list[
    ContextCompactionMiddleware | ContextSnapshotMiddleware
]:
    """Give packaged remote agents the same protection and accounting contract."""
    from deepagents.backends.filesystem import FilesystemBackend

    from novacode_cli.agents.async_workspace import workspace_root
    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware

    backend = FilesystemBackend(root_dir=str(workspace_root()), virtual_mode=True)
    return [ContextCompactionMiddleware(backend=backend), ContextSnapshotMiddleware()]
