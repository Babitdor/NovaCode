"""Episodic capture — the experience timeline (tool calls, outcomes, skills).

This module owns Nova's **Episodic memory tier**: the durable, time-ordered
record of what the agent actually did. ``tool_history`` is the timeline itself;
``tool_stats`` and ``skill_usage`` are *derived indices* over that experience.
Consolidation (the review engine and ``/dream``) replays this timeline to write
the Semantic and Procedural tiers — episodic → cortical consolidation.

Extracted from ``NovaLearningMiddleware`` to make the counter and history
independently testable.  Owns four durable-store namespaces:

- ``("nova", "tool_counter")`` → simple integer count
- ``("nova", "tool_history")`` → the episodic timeline (capped list of episodes)
- ``("nova", "tool_stats")``    → per-(non-builtin)-tool usage counters (telemetry)
- ``("nova", "skill_usage")``   → per-SKILL.md effectiveness:
  ``{skill_name: {"invocations","successes","failures","last_used"}}``

The distinction between the last two matters: ``tool_stats`` tracks *tools*
(``web_search``, ``code_search``, …), while ``skill_usage`` tracks
actual SKILL.md *skills* the agent invoked by reading their file. The
refinement loop (``check_skill_effectiveness``) reads ``skill_usage`` — keying
it off tool names (the historical bug) meant it could never match a real skill.

Read the timeline back with :meth:`ToolUsageTracker.query_episodes` (the seam a
future embedding-based associative recall plugs into).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.tracker")

_MAX_COUNTER = 1_000_000  # Sanity cap to prevent integer overflow

# A read of ``…/<something>skills…/<skill-name>/SKILL.md`` is how the agent
# invokes a skill under deepagents' progressive-disclosure model (there is no
# dedicated "load skill" tool). Capture the skill directory name.
_SKILL_READ_RE = re.compile(r"/[^/]*skills[^/]*/(?P<name>[^/]+)/SKILL\.md$", re.IGNORECASE)
# Tool names whose path argument can signal a skill read.
_READ_TOOLS: frozenset[str] = frozenset({"read_file", "read", "view", "cat"})
# How many subsequent tool calls a skill invocation "owns" for outcome
# attribution (its effectiveness window) before we stop crediting/blaming it.
# Kept deliberately tight: a long window credits/blames unrelated later work to
# the skill, which produced a noisy failure signal that drove spurious refinement.
_SKILL_ATTRIBUTION_BUDGET = 8
# How many recent failure excerpts to retain per skill (for grounded refinement).
_MAX_FAILURE_SAMPLES = 5
_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "grep",
        "ls",
        "glob",
        "execute",
        "think",
        "web_search",
        "duckduckgo_search",
        "docs_search",
        "code_search",
        "find_related_code",
        "fetch_url",
        "package_info",
    }
)


class AtomicCounter:
    """Atomic counter using asyncio lock for thread-safe operations."""

    def __init__(self, store: BaseStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def increment(self) -> int:
        """Increment counter atomically and return new value."""
        async with self._lock:
            entry = await self._store.aget(("nova", "tool_counter"), "counter")
            current = 0
            if entry and isinstance(entry.value, dict):
                current = entry.value.get("count", 0)

            new_value = min(current + 1, _MAX_COUNTER)

            await self._store.aput(("nova", "tool_counter"), "counter", {"count": new_value})
            return new_value

    async def get(self) -> int:
        """Get current counter value."""
        async with self._lock:
            entry = await self._store.aget(("nova", "tool_counter"), "counter")
            if entry and isinstance(entry.value, dict):
                return min(entry.value.get("count", 0), _MAX_COUNTER)
            return 0

    async def reset(self) -> None:
        """Reset counter to zero."""
        async with self._lock:
            await self._store.aput(("nova", "tool_counter"), "counter", {"count": 0})


class ToolUsageTracker:
    """Tracks tool-call frequency, history, and per-skill stats.

    Used by ``NovaLearningMiddleware`` to record every tool invocation and
    expose the data for review-trigger decisions.
    """

    def __init__(
        self,
        store: BaseStore,
        *,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._enabled = enabled
        self._counter = AtomicCounter(store)
        # In-memory effectiveness window: the most recently invoked skill owns
        # the outcomes of the next ``_active_skill_budget`` tool calls. Lives for
        # the process; a fresh session simply starts with no active skill.
        self._active_skill: str | None = None
        self._active_skill_budget = 0

    # -- Counter ------------------------------------------------------------

    async def get_call_count(self) -> int:
        """Return the current tool call count from the durable store."""
        if not self._enabled:
            return 0
        return await self._counter.get()

    async def increment_counter(self) -> int:
        """Increment and persist the tool call counter. Returns the new count."""
        return await self._counter.increment()

    async def reset_counter(self) -> None:
        """Reset the tool call counter to zero (after a review)."""
        await self._counter.reset()

    # -- Tool history -------------------------------------------------------

    @staticmethod
    def skill_name_from_tool_call(tool_call: dict[str, Any] | None) -> str | None:
        """Return the skill name a tool call invokes, or ``None``.

        A skill is invoked by reading its ``…/skills…/<name>/SKILL.md`` file
        (deepagents progressive disclosure — there is no dedicated load tool).
        Recognizes the common read tools and the typical path-argument keys.
        """
        if not isinstance(tool_call, dict):
            return None
        name = tool_call.get("name", "")
        if name not in _READ_TOOLS:
            return None
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            return None
        for key in ("file_path", "path", "filename", "file"):
            value = args.get(key)
            if isinstance(value, str):
                match = _SKILL_READ_RE.search(value.replace("\\", "/"))
                if match:
                    return match.group("name")
        return None

    async def record_tool_usage(
        self,
        tool_name: str,
        success: bool,
        *,
        skill_invoked: str | None = None,
        error_excerpt: str | None = None,
    ) -> None:
        """Record a single tool call in the tool history.

        When ``skill_invoked`` is set, this call *is* a skill invocation (a read
        of its SKILL.md): it opens an effectiveness window for that skill. When
        it is not, and a skill is currently active, the call's outcome is
        attributed to that active skill (success/failure within its window).
        """
        if not self._enabled:
            return
        try:
            existing = await self._store.aget(("nova", "tool_history"), "history")
            history: list[dict[str, Any]] = []
            if existing and isinstance(existing.value, dict) and "entries" in existing.value:
                history = existing.value["entries"][-199:]

            history.append(
                {
                    "tool": tool_name,
                    "success": success,
                    "timestamp": time.time(),
                }
            )
            await self._store.aput(("nova", "tool_history"), "history", {"entries": history})

            await self._track_tool_stats(tool_name, success)

            if skill_invoked:
                await self._record_skill_invocation(skill_invoked)
            elif self._active_skill and self._active_skill_budget > 0:
                self._active_skill_budget -= 1
                await self._attribute_skill_outcome(
                    self._active_skill,
                    success,
                    error_excerpt=None if success else error_excerpt,
                    tool_name=tool_name,
                )
                if self._active_skill_budget <= 0:
                    self._active_skill = None
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record tool usage for '%s'", tool_name)

    async def get_tool_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent tool usage history."""
        try:
            entry = await self._store.aget(("nova", "tool_history"), "history")
            if entry and isinstance(entry.value, dict):
                entries = entry.value.get("entries", [])
                if isinstance(entries, list):
                    return entries[-limit:]
        except Exception:  # noqa: BLE001
            logger.exception("Failed to get tool history")
        return []

    async def query_episodes(
        self,
        *,
        limit: int = 50,
        since: float | None = None,
        tool: str | None = None,
        success: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Query the episodic timeline with simple filters.

        The canonical read API over ``tool_history`` for consolidation (the
        review engine and ``/dream``) and any future recall tool. Filters are
        applied newest-last; ``limit`` caps the returned (post-filter) episodes.

        Args:
            limit: Maximum number of episodes to return (most recent kept).
            since: Only include episodes with ``timestamp >= since`` (unix secs).
            tool: Only include episodes for this tool name.
            success: Only include episodes whose outcome matches (True/False).

        Returns:
            A list of episode dicts (``{"tool", "success", "timestamp"}``),
            oldest-first, at most ``limit`` long. Empty on any error.
        """
        # Pull the full retained window (history is capped at ~200 entries), then
        # filter in memory — cheaper than a richer store query for this size.
        episodes = await self.get_tool_history(limit=10_000)
        if since is not None:
            episodes = [e for e in episodes if e.get("timestamp", 0.0) >= since]
        if tool is not None:
            episodes = [e for e in episodes if e.get("tool") == tool]
        if success is not None:
            episodes = [e for e in episodes if bool(e.get("success", True)) is success]
        return episodes[-limit:]

    # -- Tool-level telemetry -----------------------------------------------

    async def _track_tool_stats(self, tool_name: str, success: bool) -> None:
        """Track per-tool usage counters (non-builtin tools only).

        Pure telemetry about *tools* — distinct from ``skill_usage`` (which
        tracks SKILL.md effectiveness). Not used by the refinement loop.
        """
        if tool_name in _BUILTIN_TOOLS:
            return

        try:
            entry = await self._store.aget(("nova", "tool_stats"), tool_name)
            stats: dict[str, int] = {"uses": 0, "successes": 0, "failures": 0}
            if entry and isinstance(entry.value, dict):
                stats = dict(entry.value)

            stats["uses"] += 1
            if success:
                stats["successes"] += 1
            else:
                stats["failures"] += 1

            await self._store.aput(("nova", "tool_stats"), tool_name, stats)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to track tool stats for '%s'", tool_name)

    async def get_skill_stats(self) -> dict[str, dict[str, int]]:
        """Return all tracked tool-level usage statistics (telemetry)."""
        try:
            results = await self._store.asearch(("nova", "tool_stats"))
            out: dict[str, dict[str, int]] = {}
            for item in results:
                if hasattr(item, "key") and hasattr(item, "value"):
                    out[item.key] = dict(item.value)  # type: ignore[arg-type]
            return out
        except Exception:  # noqa: BLE001
            logger.exception("Failed to get tool stats")
        return {}

    # -- Skill effectiveness ------------------------------------------------

    async def _record_skill_invocation(self, skill_name: str) -> None:
        """Count a SKILL.md invocation and open its attribution window."""
        self._active_skill = skill_name
        self._active_skill_budget = _SKILL_ATTRIBUTION_BUDGET
        try:
            entry = await self._store.aget(("nova", "skill_usage"), skill_name)
            stats: dict[str, Any] = {
                "invocations": 0,
                "successes": 0,
                "failures": 0,
                "last_used": 0.0,
            }
            if entry and isinstance(entry.value, dict):
                stats.update(entry.value)
            stats["invocations"] = int(stats.get("invocations", 0)) + 1
            stats["last_used"] = time.time()
            await self._store.aput(("nova", "skill_usage"), skill_name, stats)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record skill invocation for '%s'", skill_name)

    async def _attribute_skill_outcome(
        self,
        skill_name: str,
        success: bool,
        *,
        error_excerpt: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """Credit/blame a post-invocation tool outcome to the active skill.

        On failure, also record a compact ``failure_sample`` (the tool + a
        truncated error excerpt) — capped to the most recent few. These samples
        are what makes ``refine_skill`` failure-*grounded* rather than blind.
        """
        try:
            entry = await self._store.aget(("nova", "skill_usage"), skill_name)
            stats: dict[str, Any] = {
                "invocations": 0,
                "successes": 0,
                "failures": 0,
                "last_used": 0.0,
                "failure_samples": [],
            }
            if entry and isinstance(entry.value, dict):
                stats.update(entry.value)
            if success:
                stats["successes"] = int(stats.get("successes", 0)) + 1
            else:
                stats["failures"] = int(stats.get("failures", 0)) + 1
                if error_excerpt:
                    samples = list(stats.get("failure_samples") or [])
                    samples.append(
                        {
                            "tool": tool_name or "unknown",
                            "excerpt": error_excerpt[:300],
                            "ts": time.time(),
                        }
                    )
                    stats["failure_samples"] = samples[-_MAX_FAILURE_SAMPLES:]
            await self._store.aput(("nova", "skill_usage"), skill_name, stats)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to attribute outcome to skill '%s'", skill_name)

    async def get_skill_usage(self) -> dict[str, dict[str, Any]]:
        """Return all per-skill effectiveness stats (invocations + outcomes)."""
        try:
            results = await self._store.asearch(("nova", "skill_usage"))
            out: dict[str, dict[str, Any]] = {}
            for item in results:
                if hasattr(item, "key") and hasattr(item, "value"):
                    out[item.key] = dict(item.value)  # type: ignore[arg-type]
            return out
        except Exception:  # noqa: BLE001
            logger.exception("Failed to get skill usage")
        return {}
