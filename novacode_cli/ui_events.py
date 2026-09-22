"""UI-agnostic event types for the agent stream.

These dataclasses describe *what happened* during an agent run, decoupled from
*how it is rendered*. ``execute_task`` (in :mod:`novacode_cli.ui.execution`)
produces these and hands them to a :class:`~novacode_cli.ui_renderer.StreamRenderer`,
so the same streaming/interrupt logic can drive either the legacy ``rich`` +
``prompt_toolkit`` UI or the new Textual TUI.

Display events flow one-way (agent -> renderer). Human-in-the-loop interrupts are
*not* modelled here because they require a response; those are handled by the
``request_*`` methods on the renderer protocol instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StatusUpdate:
    """Spinner / activity-indicator text. ``message=None`` hides the indicator."""

    message: str | None


@dataclass
class TextDelta:
    """An incremental chunk of assistant prose (main agent), for live streaming.

    Renderers may show these in a transient "live" region. The authoritative,
    deduplicated/markdown-rendered version arrives later as
    :class:`AssistantMessage` (commit) or is dropped via :class:`TextDiscard`
    (e.g. internal-context text that should not be shown).
    """

    text: str


@dataclass
class ReasoningDelta:
    """An incremental chunk of the model's reasoning / extended-thinking trace.

    Surfaced for observability; renderers typically show it dimmed and transient
    (it is not part of the committed answer)."""

    text: str


@dataclass
class TextDiscard:
    """The currently-streaming text was suppressed; drop any live preview."""


@dataclass
class AssistantMessage:
    """A finalized chunk of assistant prose, ready to render as markdown."""

    text: str
    agent_name: str
    agent_color: str
    is_subagent: bool = False


@dataclass
class ToolCall:
    """A tool invocation by the (main) agent."""

    name: str
    display_str: str
    icon: str
    is_main_agent: bool = True
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class ToolResult:
    """A completed tool call: a condensed preview plus the full output.

    ``preview`` is the one-line summary; ``full_output`` is the complete tool
    output so renderers can offer an expandable view. ``call_id`` ties the
    result back to its :class:`ToolCall` (for parallel tool use).
    """

    preview: str
    is_error: bool = False
    full_output: str = ""
    call_id: str | None = None


@dataclass
class FileOp:
    """A completed file operation (read/create/edit/delete).

    ``record`` is the tracker record produced by the session file-op tracker
    (see :mod:`novacode_cli.file_ops`); renderers may pass it to
    ``render_file_operation``. ``full_output`` is the tool's raw output (e.g.
    file contents) and ``call_id`` ties it to the originating :class:`ToolCall`.
    """

    record: Any
    full_output: str = ""
    call_id: str | None = None


@dataclass
class TodoUpdate:
    """The current todo list changed."""

    todos: list[dict]
    agent_name: str | None = None


@dataclass
class ErrorOutput:
    """Raw error text (e.g. a failed shell command) to surface to the user."""

    text: str


@dataclass
class CompactionNotice:
    """Context was auto-compacted during the turn."""


@dataclass
class SubagentActivity:
    """A subagent status change (dispatched / progress / completed).

    Kept intentionally loose; the legacy renderer drives the existing
    ``SubagentTracker`` directly, while the Textual renderer can show a compact
    live summary from these fields.
    """

    kind: str  # "dispatched" | "completed" | "status"
    subagent_type: str | None = None
    message: str | None = None
    detail: str | None = None
    color: str | None = None
    call_id: str | None = None  # tool call id for matching dispatch ↔ completion


@dataclass
class UsageUpdate:
    """Token usage captured from the model response (main agent only)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class InterruptRequest:
    """A human-in-the-loop request that needs a response before resuming.

    The consumer fulfils ``future`` with the decision; ``run_agent_stream``
    awaits it and resumes the graph with the result.

    - kind ``"tool"``    -> payload is a ``HITLRequest``; response is
      ``{"decisions": [...], "any_rejected": bool}``.
    - kind ``"question"``-> payload is the question request dict; response is the
      HITL response object to resume with.
    - kind ``"plan"``    -> payload is the current todos list; response is
      ``{"response": <HITLResponse>, "state_update": dict}``.
    """

    kind: str  # "tool" | "question" | "plan"
    payload: Any
    future: "asyncio.Future[Any]"


@dataclass
class Done:
    """The agent run finished normally."""

    had_response: bool = False


@dataclass
class ContextMessage:
    """System-level context message (not user or assistant text).

    Used for things like review cycle notifications, skill creation, or other
    system-level status messages that should appear in the UI but are not part
    of the direct conversation flow.
    """

    message: str
    event_type: str = ""
    """Semantic type like 'nova_review_start', 'nova_review_complete', etc."""
    icon: str = "•"
    color: str = "cyan"


@dataclass
class Cancelled:
    """The run was cancelled (Ctrl+C / Escape) and cleaned up."""


@dataclass
class ContextOverflow:
    """The provider rejected the request because the context was too long.

    Distinct from :class:`Error` because it is *recoverable in one specific
    way*: compact the conversation and retry. Plain backoff would fail
    identically, so renderers that can compact (the TUI) should do that and
    retry once; renderers that cannot should show ``message`` like any other
    provider notice.
    """

    message: str
    exception: BaseException | None = None


@dataclass
class Error:
    """The run failed with an error to surface.

    ``is_provider_notice`` marks a recognised model-provider failure (usage/rate
    limit, auth, connectivity) whose ``message`` has already been formatted into
    a clean, actionable notice by :func:`novacode_cli.errors.friendly_model_error`.
    Renderers use it to style the message as a warning rather than a raw error.
    """

    message: str
    exception: BaseException | None = None
    is_provider_notice: bool = False
