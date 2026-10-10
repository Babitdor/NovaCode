"""Nova learning middleware — tool call tracking, review triggers, and lesson extraction.

This middleware implements the core "Periodic Review Loop" pillar of the Nova
system. It wraps every tool call to count usage, and after a threshold of calls
injects review instructions into the next model call.

Architecture:
    Follows the same AgentMiddleware pattern as AgentMemoryMiddleware and
    SteeringMiddleware (see ``novacode_cli/memory/agent_memory.py`` and
    ``novacode_cli/bootstrap/steering.py``).  Uses the shared durable store
    for persistence across restarts.

    This module is a **thin orchestrator** that delegates to three focused
    modules extracted from the original god-middleware:

    - :mod:`novacode_cli.hermes.tracker` — ``ToolUsageTracker``
      (counter, tool history, skill stats)
    - :mod:`novacode_cli.hermes.review` — ``ReviewRunner``
      (trigger, OOB invoke, content persistence)
    - :mod:`novacode_cli.hermes.skill_manager` — ``SkillManager``
      (create/cleanup/refine skills)

Durable Store Schema (namespace="Nova"):
    ("nova", "tool_counter")    → {"count": int}
    ("nova", "tool_history")    → [{"tool": str, "success": bool, "timestamp": float}, ...]
    ("nova", "last_review")     → {"timestamp": float, "lessons": list[str], "lessons_str": str}
    ("nova", "skill_stats")     → {skill_name: {"uses": int, "successes": int, "failures": int}}
    ("nova", "reviews")         → {review_id: {"timestamp": float, "content": str}}
    ("nova", "meta")            → {"last_review": float, "review_count": int, "review_just_completed": bool}
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import NotRequired

from novacode_cli.hermes import config as _config

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.middleware")

# Tools whose textual output can reveal a *latent* failure: the call returned
# status="success" (the tool ran) but the command it ran actually failed.
_EXECUTE_TOOLS: frozenset[str] = frozenset(
    {"execute", "run_tests", "shell", "bash", "run_command"}
)
# High-precision failure markers in shell/test output. Kept conservative to
# avoid false positives (e.g. we match "3 failed" but not "0 failed").
_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\bcommand not found\b", re.IGNORECASE),
    re.compile(r"is not recognized as (?:an |the name of a )?", re.IGNORECASE),
    re.compile(r"^fatal:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"npm err!", re.IGNORECASE),
    re.compile(r"\b[1-9]\d* failed\b", re.IGNORECASE),
    re.compile(r"\b[1-9]\d* error(?:s)?\b", re.IGNORECASE),
)


def _content_to_text(response: Any) -> str:
    """Best-effort flatten of a tool response's content to a string."""
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _execution_failed(tool_name: str, response: Any) -> bool:
    """Detect a latent failure in an execute/test tool's output.

    Only applies to shell-like tools; everything else returns ``False`` (their
    ``status`` field is authoritative). Conservative by design.
    """
    if tool_name not in _EXECUTE_TOOLS or response is None:
        return False
    text = _content_to_text(response)
    if not text:
        return False
    return any(p.search(text) for p in _FAILURE_PATTERNS)


class NovaState(AgentState):
    """Extended agent state for tracking Nova learning cycle."""

    nova_review_triggered: NotRequired[bool]
    """Whether a review prompt was injected into the current model call."""
    last_review_timestamp: NotRequired[float]
    """Unix timestamp of the most recent review."""
    review_count: NotRequired[int]
    """Total number of reviews completed in this session."""
    last_review_content: NotRequired[str]
    """Preview of the last review content."""


# The live middleware instance, so callers outside the graph can reach it.
# LangGraph compiles middleware into the graph and does not expose the
# instances, but session-end consolidation has to call the same object that
# holds this session's tracker and last request. Mirrors the
# get_shared_mcp_middleware() pattern in novacode_cli/mcp/__init__.py.
_active_middleware: "NovaLearningMiddleware | None" = None


def get_active_learning_middleware() -> "NovaLearningMiddleware | None":
    """The learning middleware for the current agent, if one was built."""
    return _active_middleware


# Several agents can be alive in one process (a harness running trials side by
# side), and "the active one" is then whichever was built last. Each registers
# under its project key so its owner can reach that instance specifically.
_by_project: "dict[str, NovaLearningMiddleware]" = {}


def learning_middleware_for(project: str) -> "NovaLearningMiddleware | None":
    """The learning middleware built for ``project``, if it is still registered."""
    return _by_project.get(project)


def forget_learning_middleware(project: str) -> None:
    """Drop a finished project's middleware from the registry."""
    _by_project.pop(project, None)


class NovaLearningMiddleware(AgentMiddleware[NovaState]):
    """Track tool usage, trigger periodic reviews, and manage learning state.

    Thin orchestrator that delegates all business logic to:
    - ``self._tracker`` — :class:`~novacode_cli.hermes.tracker.ToolUsageTracker`
    - ``self._review`` — :class:`~novacode_cli.hermes.review.ReviewRunner`
    - ``self._skill_manager`` — :class:`~novacode_cli.hermes.skill_manager.SkillManager`
    """

    state_schema = NovaState
    tools: list = []  # type: ignore

    def __init__(
        self,
        store: BaseStore,
        *,
        review_threshold: int = _config.REVIEW_THRESHOLD_DEFAULT,
        skills_dir: Path | None = None,
        agent_dir: Path | None = None,
        enabled: bool = True,
        project: str | None = None,
        review_mode: str | None = None,
    ) -> None:
        """Initialize the Nova learning middleware.

        Args:
            store: Shared durable store (from ``get_durable_store()``).
            review_threshold: Number of tool calls between automatic reviews.
            skills_dir: Path to the skills directory (for skill creation/refinement).
            agent_dir: Path to the agent directory (for USER.md / MEMORY.md).
            enabled: If False, all hooks are no-ops.
            project: Key of the codebase being worked in (see
                ``memory_tiers.project_memory_key``). Lessons that only hold
                there are kept apart from the ones worth carrying everywhere.
        """
        super().__init__()
        self._store = store
        self._review_threshold = review_threshold
        self._skills_dir = skills_dir
        self._agent_dir = agent_dir
        self._enabled = enabled

        # Lazy-import the extracted modules to keep startup footprint small.
        from novacode_cli.hermes.evolution import EvolutionEngine
        from novacode_cli.hermes.review import ReviewRunner
        from novacode_cli.hermes.skill_manager import SkillManager
        from novacode_cli.hermes.tracker import ToolUsageTracker

        self._skill_manager = SkillManager(
            store,
            skills_dir=skills_dir,
            enabled=enabled,
            project=project,
        )
        self._tracker = ToolUsageTracker(
            store,
            enabled=enabled,
        )
        self._review = ReviewRunner(
            store,
            self._tracker,
            self._skill_manager,
            review_threshold=review_threshold,
            agent_dir=agent_dir,
            enabled=enabled,
            project=project,
            mode=review_mode,
        )
        # Self-evolution: complex-task completion unlocks / levels up a skill.
        self._evolution = EvolutionEngine(
            store,
            self._tracker,
            self._skill_manager,
            skills_dir=skills_dir,
            enabled=enabled,
        )
        # Start time of the current task (set in abefore_agent), used to slice
        # the task's episodic window in aafter_agent.
        self._task_start_ts: float | None = None
        # Last ModelRequest seen, retained so a session-end consolidation can
        # run a review without a live turn to hang off (see
        # consolidate_session). run_review only needs .messages and .model.
        self._last_request: Any = None

        # Publish for get_active_learning_middleware(). The graph owns the
        # instance once compiled, so /quit and /clear have no other way to
        # reach this session's tracker.
        global _active_middleware
        _active_middleware = self
        if project:
            _by_project[project] = self

    @property
    def _refinement_tasks(self) -> set[asyncio.Task]:
        """Background review/skill tasks (owned by the SkillManager).

        Exposed on the middleware for callers/tests that drain pending work
        without reaching into the delegated ``SkillManager``.
        """
        return self._skill_manager._refinement_tasks

    # ------------------------------------------------------------------
    # Counter delegation
    # ------------------------------------------------------------------

    async def _get_tool_call_count(self) -> int:
        return await self._tracker.get_call_count()

    async def _set_tool_call_count(self, count: int) -> None:
        await self._store.aput(
            ("nova", "tool_counter"),
            "counter",
            {"count": min(count, 1_000_000)},
        )

    async def _increment_counter(self) -> int:
        return await self._tracker.increment_counter()

    async def _reset_counter(self) -> None:
        await self._tracker.reset_counter()
        await self._review._set_review_just_completed(True)

    # ------------------------------------------------------------------
    # Tool usage delegation
    # ------------------------------------------------------------------

    async def _record_tool_usage(self, tool_name: str, success: bool) -> None:
        await self._tracker.record_tool_usage(tool_name, success)

    async def get_tool_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent tool usage history."""
        return await self._tracker.get_tool_history(limit=limit)

    async def get_skill_stats(self) -> dict[str, dict[str, int]]:
        """Return all tracked skill usage statistics."""
        return await self._tracker.get_skill_stats()

    # ------------------------------------------------------------------
    # Review delegation
    # ------------------------------------------------------------------

    async def _should_review(self) -> bool:
        return await self._review.should_review()

    async def _run_review(self, request: ModelRequest) -> None:
        await self._review.run_review(request)

    async def _apply_review_content(self, content: str) -> None:
        await self._review._apply_review_content(content)

    async def _get_review_count(self) -> int:
        return await self._review._get_review_count()

    async def _get_review_just_completed(self) -> bool:
        return await self._review._get_review_just_completed()

    async def _set_review_just_completed(self, value: bool) -> None:
        await self._review._set_review_just_completed(value)

    # ------------------------------------------------------------------
    # Skill delegation
    # ------------------------------------------------------------------

    async def _maybe_create_skill_from_review(self, review_text: str) -> None:
        await self._skill_manager.maybe_create_from_review(review_text)

    async def _cleanup_legacy_skills_once(self) -> None:
        await self._skill_manager.cleanup_legacy_skills_once()

    async def _maybe_refine_skills(self) -> None:
        await self._skill_manager.maybe_refine_skills()

    # ------------------------------------------------------------------
    # TUI event emission
    # ------------------------------------------------------------------

    async def _emit_tui_event(self, event_type: str, message: str) -> None:
        from novacode_cli.hermes.review import _emit_event

        _emit_event(event_type, message)

    # ------------------------------------------------------------------
    # Middleware hooks
    # ------------------------------------------------------------------

    # -- Task lifecycle (self-evolution) ------------------------------------

    def before_agent(self, state: AgentState, runtime: Any = None) -> None:  # noqa: ARG002
        """Synchronous hook — mark task start (pass-through)."""
        self._task_start_ts = time.time()

    async def abefore_agent(self, state: AgentState, runtime: Any = None) -> None:  # noqa: ARG002
        """Mark the start of a task (used to slice its episodic window)."""
        self._task_start_ts = time.time()

    def after_agent(self, state: AgentState, runtime: Any = None) -> None:  # noqa: ARG002
        """Synchronous hook — pass-through (evolution runs async)."""

    async def consolidate_session(self, *, timeout: float = 45.0) -> bool:
        """Distil un-reviewed work into durable memory before the session ends.

        Nova's learning is event-driven: a review fires on tool-call count or a
        failure burst. A session that finishes real work *below* that threshold
        — six edits and quit — never reviews, so its lessons are lost at exit.
        This is the missing session-end trigger; it reuses the normal review
        path (and therefore the normal ``record_lesson`` /
        ``update_user_model`` writes) rather than adding a second learning
        surface.

        Called from ``/quit`` and ``/clear``. Bounded by *timeout* and never
        raises: exiting must not hang or fail on a consolidation.

        Returns:
            True if a consolidation review actually ran.
        """
        if not self._enabled or self._last_request is None:
            return False

        try:
            if not await self._review.should_consolidate():
                return False
        except Exception:  # noqa: BLE001 - never block exit on the check
            logger.debug("consolidation check failed", exc_info=True)
            return False

        try:
            await asyncio.wait_for(
                self._review.run_review(self._last_request), timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.debug("session consolidation timed out after %.0fs", timeout)
            return False
        except Exception:  # noqa: BLE001 - a failed consolidation must not break exit
            logger.debug("session consolidation failed", exc_info=True)
            return False

        try:
            await self._tracker.reset_counter()
        except Exception:  # noqa: BLE001
            pass
        return True

    async def aafter_agent(self, state: AgentState, runtime: Any = None) -> None:  # noqa: ARG002
        """At task completion, maybe evolve a skill from a complex task.

        Fires only for the main agent (subagents don't carry this middleware),
        so it is exactly one trigger per user task. Never raises.
        """
        if not self._enabled:
            return
        try:
            await self._evolution.maybe_evolve(dict(state), self._task_start_ts)
        except Exception:  # noqa: BLE001
            logger.debug("aafter_agent evolution failed", exc_info=True)
        await self._review_finished_task(state)

    async def _review_finished_task(self, state: AgentState) -> None:
        """Review the task that just ended, out of band. Never raises.

        This is the main review in "task_end" mode: it sees the whole task,
        including how it turned out, and it happens once. It runs as a
        background task so the user's turn ends when the agent's answer does.
        """
        if getattr(self._review, "_mode", "") != "task_end" or self._last_request is None:
            return
        try:
            if not await self._review.should_consolidate():
                return
            messages = dict(state).get("messages") if state is not None else None
            request = (
                self._last_request.override(messages=list(messages))
                if messages
                else self._last_request
            )
            await self._tracker.reset_counter()
            task = asyncio.create_task(self._review.run_review_task(request))
            self._skill_manager._refinement_tasks.add(task)
            task.add_done_callback(self._skill_manager._refinement_tasks.discard)
        except Exception:  # noqa: BLE001
            logger.debug("end-of-task review failed to start", exc_info=True)

    async def drain(self, timeout: float = 60.0) -> None:
        """Wait for reviews still running in the background. Never raises."""
        tasks = list(self._skill_manager._refinement_tasks)
        if not tasks:
            return
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception:  # noqa: BLE001
            logger.debug("draining reviews failed", exc_info=True)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Synchronous wrapper — pass-through only."""
        return handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Synchronous wrapper — pass-through only."""
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Track every tool call: increment counter and record usage."""
        if not self._enabled:
            return await handler(request)

        tool_call = getattr(request, "tool_call", None)
        skill_invoked = self._tracker.skill_name_from_tool_call(tool_call)
        try:
            response = await handler(request)
            tool_name = (
                tool_call.get("name", "unknown")
                if isinstance(tool_call, dict)
                else "unknown"
            )
            status_ok = (
                (getattr(response, "status", "success") != "error")
                if response
                else True
            )
            # A shell/test call can report status="success" yet have actually
            # failed (non-zero exit, traceback, N failed tests). Inspect output.
            success = status_ok and not _execution_failed(tool_name, response)
            await self._tracker.increment_counter()
            await self._tracker.record_tool_usage(
                tool_name,
                success,
                skill_invoked=skill_invoked,
                error_excerpt=None if success else _content_to_text(response)[:300],
            )
            return response
        except Exception as exc:
            tool_name = (
                tool_call.get("name", "unknown")
                if isinstance(tool_call, dict)
                else "unknown"
            )
            await self._tracker.increment_counter()
            await self._tracker.record_tool_usage(
                tool_name,
                False,
                skill_invoked=skill_invoked,
                error_excerpt=f"{type(exc).__name__}: {exc}"[:300],
            )
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Run the agent's turn, then trigger an out-of-band review if due.

        The agent's real model call is ALWAYS executed and its response is
        returned untouched, so the agent loop continues normally. When the tool
        call count has reached the threshold, the review runs as a separate
        ``model.ainvoke`` — it never replaces the agent's task turn.
        """
        if not self._enabled:
            return await handler(request)

        # Retained for consolidate_session (session end has no live request).
        self._last_request = request

        should_review = await self._review.should_review()
        response = await handler(request)

        if should_review:
            await self._tracker.reset_counter()
            await self._review._set_review_just_completed(True)

            from novacode_cli.hermes.review import _emit_event

            _emit_event(
                "nova_review_start",
                "◐ Nova review cycle starting...",
            )
            task = asyncio.create_task(self._review.run_review_task(request))
            self._skill_manager._refinement_tasks.add(task)
            task.add_done_callback(self._skill_manager._refinement_tasks.discard)

        return response
