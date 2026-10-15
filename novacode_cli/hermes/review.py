"""Review orchestration — out-of-band model invocation, content persistence.

Extracted from ``NovaLearningMiddleware``.  Owns the periodic review lifecycle:
triggering when the tool-call threshold is reached, running a separate model
call, parsing the result, and persisting learnings to memory files + durable
store.

Depends on ``ToolUsageTracker`` for counter resets and ``SkillManager`` for
post-review skill creation/refinement (both injected).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import SystemMessage

from novacode_cli.events import cap_event_log, nova_event_log
from novacode_cli.hermes import config
from novacode_cli.prompts import render_template

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest
    from langgraph.store.base import BaseStore

    from novacode_cli.hermes.skill_manager import SkillManager
    from novacode_cli.hermes.tracker import ToolUsageTracker

logger = logging.getLogger("nova.hermes.review")
REVIEW_INPUT_CAP = 16_000
_REVIEW_SLOTS = threading.BoundedSemaphore(2)

# Tools that signal *real work* happened in a window (vs. read-only browsing).
# Any non-builtin tool (a skill or MCP tool) also counts as substantive.
_SUBSTANTIVE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "execute", "run_tests"})
# Read-only/builtin tools that, on their own, are not worth reviewing.
_TRIVIAL_BUILTINS: frozenset[str] = frozenset(
    {
        "read_file",
        "grep",
        "ls",
        "glob",
        "think",
        "web_search",
        "duckduckgo_search",
        "docs_search",
        "fetch_url",
        "package_info",
    }
)
# A burst of failures in the window is worth reviewing *early* (before the count
# threshold), so the agent captures the recovery while it's fresh. Sourced from
# the central config so the threshold tuner (Enhancement 4) has one home for it;
# ReviewRunner prefers a store-tuned value at runtime (see __init__).
_FAILURE_BURST = config.FAILURE_BURST_DEFAULT


_SHELL_TOOLS = frozenset({"bash", "shell", "execute"})
# Output that reads like a check having been run: a test runner, a build, an
# assertion, an explicit verdict.
_CHECK_RE = re.compile(
    r"\b\d+ (?:passed|failed|errors?)\b|\bPASS(?:ED)?\b|\bFAIL(?:ED|URE)?\b|\bOK\b"
    r"|Traceback \(most recent call last\)|AssertionError|\bTests? (?:passed|failed)\b"
    r"|exit(?:ed with)? code:? ?\d+|\berror:|compil(?:ed|ation)",
    re.IGNORECASE,
)
# How many of the most recent shell results to look through for the last check.
_OUTCOME_LOOKBACK = 8


def session_outcome(messages: list[Any]) -> tuple[str, str]:
    """How the work stood when it was last checked: ``(status, evidence)``.

    ``status`` is "passed", "failed" or "unknown". It is read from the session's
    own tool results — the most recent shell output that looks like a check —
    because a review that is not told the outcome writes down whatever approach
    is on screen as though it had worked.
    """
    from novacode_cli.hermes.middleware import _FAILURE_PATTERNS, _content_to_text

    shell_results = []
    for msg in messages:
        if getattr(msg, "type", None) != "tool":
            continue
        if (getattr(msg, "name", None) or "") not in _SHELL_TOOLS:
            continue
        text = _content_to_text(msg)
        if text:
            shell_results.append(text)
    for text in reversed(shell_results[-_OUTCOME_LOOKBACK:]):
        if not _CHECK_RE.search(text):
            continue
        failed = any(p.search(text) for p in _FAILURE_PATTERNS)
        return ("failed" if failed else "passed"), " ".join(text.split())[-240:]
    return "unknown", ""


def outcome_text(status: str, evidence: str) -> str:
    """The outcome as the review prompt states it."""
    if status == "failed":
        return (
            "The most recent check in this session FAILED:\n"
            f"    {evidence}\n"
            "The approach on screen is unproven. Record only pitfalls you actually hit, "
            "with scope `project`, and do not propose a skill."
        )
    if status == "passed":
        return (
            "The most recent check in this session passed:\n"
            f"    {evidence}\n"
            "What led to that result may be recorded as having worked."
        )
    return (
        "No test, build or other check was run in this session, so nothing in it is "
        "verified. Record observations and pitfalls, not solutions."
    )


def _window_recovered(window: list[dict]) -> bool:
    """True if the window shows an error *that was then recovered from*.

    Defined as: at least one failed call, followed later by a successful
    *substantive* call (a write/edit/execute/test or non-builtin tool). That
    "hit an error, then made it work" shape is exactly what's worth distilling
    into a skill.
    """
    last_fail = max(
        (i for i, h in enumerate(window) if not h.get("success", True)),
        default=-1,
    )
    if last_fail < 0:
        return False
    for h in window[last_fail + 1 :]:
        if h.get("success", True) and (
            h.get("tool") in _SUBSTANTIVE_TOOLS or h.get("tool") not in _TRIVIAL_BUILTINS
        ):
            return True
    return False


# Icon / color mapping for TUI events emitted by this module.
_EVENT_CONFIG: dict[str, dict[str, str]] = {
    "nova_review_start": {"icon": "◐", "color": "cyan"},
    "nova_review_complete": {"icon": "✓", "color": "green"},
    "nova_skill_created": {"icon": "●", "color": "green"},
    "nova_skill_refinement": {"icon": "◐", "color": "yellow"},
}


def _emit_event(event_type: str, message: str) -> None:
    """Append a Nova event to the shared module-level buffer.

    Best-effort — never raise from a notification.
    """
    try:
        conf = _EVENT_CONFIG.get(event_type, {"icon": "•", "color": "cyan"})
        nova_event_log.append((event_type, conf["icon"], conf["color"], message))
        cap_event_log()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to emit TUI event '%s'", event_type)


class ReviewRunner:
    """Periodic review lifecycle: trigger, run, persist.

    Designed to be created by ``NovaLearningMiddleware`` which injects the
    ``ToolUsageTracker`` and ``SkillManager`` instances.
    """

    def __init__(
        self,
        store: BaseStore,
        tracker: ToolUsageTracker,
        skill_manager: SkillManager,
        *,
        review_threshold: int = config.REVIEW_THRESHOLD_DEFAULT,
        agent_dir: Path | None = None,
        enabled: bool = True,
        project: str | None = None,
        mode: str | None = None,
    ) -> None:
        self._store = store
        # Key of the codebase being worked in; scopes lessons (see memory_tiers).
        self._project = project
        # "task_end" or "periodic" (see config.REVIEW_MODE_DEFAULT).
        self._mode = mode or config.REVIEW_MODE_DEFAULT
        # (status, evidence) of the session's last check, as of the last review.
        self._outcome: tuple[str, str] = ("unknown", "")
        self._tracker = tracker
        self._skill_manager = skill_manager
        self._review_threshold = review_threshold
        # Tuner-tunable runtime thresholds. Seeded from config; overridden once
        # (lazily, on the first should_review) by any value the ThresholdTuner
        # has persisted to the store. See _ensure_thresholds_loaded.
        self._failure_burst = config.FAILURE_BURST_DEFAULT
        self._thresholds_loaded = False
        self._agent_dir = agent_dir
        self._enabled = enabled
        self._running = False
        # Set by should_review when a clean-win review fires; read (and cleared)
        # by run_review to render the good-habit prompt section.
        self._pending_clean_win = False

    # -- Trigger decision ---------------------------------------------------

    async def should_review(self) -> bool:
        """Decide whether to trigger a review — by *signal*, not raw count.

        A review fires when any of these hold (and a review wasn't just done):

        - **Failure burst**: >= ``_FAILURE_BURST`` failed tool calls in the
          current window (review early, while the recovery is fresh).
        - **Substantive threshold**: count >= threshold *and* the window
          contains real work (a write/edit/execute/test or any non-builtin
          tool) — not just read-only browsing.
        - **Hard cap**: count >= 2× threshold, so an all-trivial window can't
          defer a review forever.

        A count-only window of pure reads is *deferred* (not reviewed), since
        there's nothing worth distilling. When window history is unavailable we
        treat it as substantive, preserving plain count-based behavior.
        """
        await self._ensure_thresholds_loaded()
        count = await self._tracker.get_call_count()
        min_floor = max(3, self._review_threshold // 2)

        # Inspect the current window (calls since the last counter reset).
        window: list[dict] = []
        if count > 0:
            try:
                window = await self._tracker.get_tool_history(limit=count)
            except Exception:  # noqa: BLE001
                window = []

        failures = sum(1 for h in window if not h.get("success", True))
        # Only suppress when we positively know the window was all-trivial.
        substantive = (not window) or any(
            (h.get("tool") in _SUBSTANTIVE_TOOLS) or (h.get("tool") not in _TRIVIAL_BUILTINS)
            for h in window
        )

        failure_burst = failures >= self._failure_burst and count >= min_floor
        reached = count >= self._review_threshold
        hard_cap = count >= 2 * self._review_threshold
        # A clean win: real substantive work, zero failures, past the floor. The
        # window is non-empty here because count >= min_floor (>= 3).
        clean_win = bool(window) and substantive and failures == 0 and count >= min_floor

        if self._mode == "task_end":
            # Mid-task, the only thing worth an extra model call is an error the
            # agent has just worked through: that is fresh, specific, and already
            # has its outcome. Everything else waits for the end of the task.
            if not (failure_burst and _window_recovered(window)):
                return False
        elif not (hard_cap or failure_burst or (reached and substantive) or clean_win):
            return False

        just_completed = await self._get_review_just_completed()
        if just_completed:
            await self._set_review_just_completed(False)
            return False

        self._pending_clean_win = clean_win
        return True

    async def _ensure_thresholds_loaded(self) -> None:
        """Load tuner-adjusted thresholds from the store, once per process.

        The :class:`~novacode_cli.hermes.tuner.ThresholdTuner` persists tuned
        values to :data:`config.HARNESS_CONFIG_NS`; this picks them up on the
        first :meth:`should_review`. Absent or malformed entries leave the
        config defaults in place, so a fresh install behaves exactly as before.
        Values are clamped defensively against a hand-edited store.
        """
        if self._thresholds_loaded:
            return
        # Set the flag first so a transient store error doesn't re-hit the store
        # on every single should_review call for the rest of the session.
        self._thresholds_loaded = True
        try:
            entry = await self._store.aget(config.HARNESS_CONFIG_NS, config.HARNESS_CONFIG_KEY)
            if entry and isinstance(entry.value, dict):
                rt = entry.value.get("review_threshold")
                fb = entry.value.get("failure_burst")
                if isinstance(rt, int):
                    self._review_threshold = max(
                        config.REVIEW_THRESHOLD_FLOOR,
                        min(config.REVIEW_THRESHOLD_CEILING, rt),
                    )
                if isinstance(fb, int):
                    self._failure_burst = max(
                        config.FAILURE_BURST_FLOOR,
                        min(config.FAILURE_BURST_CEILING, fb),
                    )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load tuned thresholds; using defaults")

    async def reset_after_review(self) -> None:
        """Reset the counter and mark review-just-completed."""
        await self._tracker.reset_counter()
        await self._set_review_just_completed(True)

    # -- OOB model invoke ---------------------------------------------------

    async def run_review(self, request: ModelRequest) -> None:
        """Compatibility entry point using the bounded preparation lifecycle."""
        await self.run_review_task(request)

    async def _prepare_review(self, request: ModelRequest) -> None:
        """Run a self-review as a *separate, out-of-band* model call.

        CRITICAL: the review must NOT be injected into the agent's task turn.
        Instead we issue an independent ``model.ainvoke`` with the conversation
        history plus a review prompt, parse the result, and persist learnings.
        The agent's real turn is returned untouched by the caller.
        """
        if not self._enabled:
            return

        try:
            prior_lessons = await self._recent_lessons()
            # The counter is reset before this task runs, so look back a fixed
            # recent window (history isn't reset) to detect an error-recovery.
            try:
                import json
                frozen = (getattr(request, "state", None) or {}).get("_nova_review_episodes")
                recent = json.loads(frozen) if frozen is not None else await self._tracker.get_tool_history(limit=2 * self._review_threshold)
            except Exception:  # noqa: BLE001
                recent = []
            recovered_from_error = _window_recovered(recent)
            task_id = recent[-1].get("task", self._tracker.task) if recent else self._tracker.task
            evidence = tuple(
                f"episode:{e.get('event_id', 'legacy')}:{e.get('outcome', 'unknown')}:"
                f"{e.get('evidence_source', 'fallback')}"
                for e in recent
            )
            clean_win = self._pending_clean_win
            self._pending_clean_win = False
            self._outcome = session_outcome(request.messages)
            review_content = render_template(
                "nova_review.jinja",
                tool_call_count=self._review_threshold,
                prior_lessons=prior_lessons,
                recovered_from_error=recovered_from_error,
                clean_win=clean_win,
                existing_topics=self._existing_topics(),
                outcome=outcome_text(*self._outcome),
            )
            messages = [*request.messages, SystemMessage(content=review_content)]

            # Name + tag + annotate this call so it's findable in LangSmith.
            # The review runs in a fire-and-forget asyncio task, so it surfaces
            # as its OWN top-level trace (not nested under the agent run) — a
            # generic name makes it impossible to pick out. With this config it
            # is filterable via:  langsmith trace list --name nova_oob_review
            review_count = await self._get_review_count()
            review_config = {
                "run_name": "nova_oob_review",
                "tags": ["nova", "hermes", "oob-review"],
                "metadata": {
                    # Marks this as an out-of-band call so iterate_agent_events
                    # drops its streamed output instead of rendering it as a
                    # "Nova" assistant message (it inherits the graph's stream
                    # callback via contextvars). See agent_loop's nova_oob filter.
                    "nova_oob": True,
                    "nova_review_count": review_count,
                    "nova_review_threshold": self._review_threshold,
                    "nova_window_messages": len(request.messages),
                    "nova_prior_lessons": bool(prior_lessons),
                },
            }
            from langchain_core.messages.utils import count_tokens_approximately

            from novacode_cli.tracking.request_metrics import dispatch

            if count_tokens_approximately(messages) > REVIEW_INPUT_CAP or not recent:
                return
            # bind caps the auxiliary request independently of the coding turn.
            model = request.model
            module = type(model).__module__
            from langchain_core.language_models.fake_chat_models import FakeListChatModel
            if not module.startswith(("langchain_openai.", "langchain_anthropic.",
                                      "langchain_core.language_models.fake")) and not isinstance(model, FakeListChatModel):
                logger.info("Learning review skipped: adapter retry/output caps unavailable")
                return
            if "max_retries" in getattr(type(model), "model_fields", {}):
                model = model.model_copy(update={"max_retries": 1})
                # Cached SDK clients otherwise retain the coding model's retry
                # policy even after copying its max_retries field.
                for name in ("_client", "_async_client"):
                    model.__dict__.pop(name, None)
                for name in ("root_client", "root_async_client"):
                    client = getattr(model, name, None)
                    if client is not None and hasattr(client, "with_options"):
                        bounded = client.with_options(max_retries=1)
                        setattr(model, name, bounded)
                        resource = "async_client" if name == "root_async_client" else "client"
                        setattr(model, resource, bounded.chat.completions)
            reviewer = model.bind(max_tokens=4096)
            with dispatch("learning_review", task_id=self._tracker.owner):
                review_msg = await reviewer.ainvoke(messages, config=review_config)

            content = ""
            raw = getattr(review_msg, "content", "")
            content = raw if isinstance(raw, str) else str(raw)

            await self._apply_review_content(content, task_id=task_id, evidence=evidence)
        except Exception as e:  # noqa: BLE001
            # A known provider problem (region opt-in, auth, quota) on the review's
            # out-of-band model call means it can't run right now — one clean line,
            # not a per-cycle traceback. Unknown failures keep the full trace.
            from novacode_cli.errors import friendly_model_error

            notice = friendly_model_error(e)
            if notice:
                logger.warning("Nova review skipped: %s", notice.splitlines()[0])
            else:
                logger.exception("Nova out-of-band review failed")

    async def should_consolidate(self) -> bool:
        """Is there un-reviewed substantive work worth distilling before exit?

        The periodic trigger fires on tool-call *count* (``should_review``), so a
        session that does real work and ends below the threshold — six edits and
        quit — never reviews, and everything it learned is lost. This is the
        session-end catch: it asks only whether the current window contains real
        work that has not been reviewed yet, with no threshold at all.

        Deliberately narrower than ``should_review``: no failure-burst or
        hard-cap paths (those already fired mid-session if they applied), and a
        review that just completed means there is nothing left to distill.
        """
        if not self._enabled:
            return False

        count = await self._tracker.get_call_count()
        if count <= 0:
            return False  # nothing since the last review

        try:
            window = await self._tracker.get_tool_history(limit=count)
        except Exception:  # noqa: BLE001
            window = []

        # Same definition of "real work" the periodic trigger uses. An unknown
        # window (history unavailable) counts as substantive, matching
        # should_review's bias toward reviewing rather than silently skipping.
        substantive = (not window) or any(
            (h.get("tool") in _SUBSTANTIVE_TOOLS) or (h.get("tool") not in _TRIVIAL_BUILTINS)
            for h in window
        )
        if not substantive:
            return False

        return not await self._get_review_just_completed()

    async def run_review_task(self, request: ModelRequest) -> None:
        """Background wrapper around :meth:`run_review`.

        Emits the completion event in a ``finally`` so the live indicator is
        always cleared/updated even if the review call fails.
        """
        if self._running:
            return
        self._running = True
        acquired = False
        try:
            snapshot = request.override(messages=deepcopy(request.messages))
            async with asyncio.timeout(60):
                while not _REVIEW_SLOTS.acquire(blocking=False):
                    await asyncio.sleep(0.05)
                acquired = True
                await self._prepare_review(snapshot)
        except TimeoutError:
            logger.info("Learning review skipped: 60-second deadline")
        finally:
            if acquired:
                _REVIEW_SLOTS.release()
            self._running = False
            _emit_event("nova_review_complete", "✓ Nova review cycle complete")

    def _existing_topics(self) -> str:
        """Topic names already on file, for the reviewer to reuse. Best-effort."""
        if not self._agent_dir:
            return ""
        try:
            from novacode_cli.hermes.memory_tiers import existing_topics

            return ", ".join(existing_topics(self._agent_dir, self._project))
        except Exception:  # noqa: BLE001
            return ""

    async def _recent_lessons(self, limit: int = 5, max_chars: int = 1500) -> str:
        """Return a digest of recent prior reviews to suppress repeat lessons.

        Pulls the most recent ``self._namespace("reviews")`` entries (newest first) and
        concatenates their content up to ``max_chars``. Injected into the review
        prompt so the LLM doesn't re-derive lessons already captured. Best-effort.
        """
        try:
            results = await self._store.asearch(self._namespace("reviews"))
        except Exception:  # noqa: BLE001
            return ""

        entries: list[tuple[float, str]] = []
        for item in results or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict) and value.get("content"):
                entries.append((value.get("timestamp", 0.0), str(value["content"])))

        entries.sort(key=lambda e: e[0], reverse=True)

        digest: list[str] = []
        used = 0
        for _ts, content in entries[:limit]:
            snippet = content.strip()
            if not snippet:
                continue
            if used + len(snippet) > max_chars:
                snippet = snippet[: max_chars - used]
            digest.append(snippet)
            used += len(snippet)
            if used >= max_chars:
                break
        return "\n\n---\n\n".join(digest)

    # -- Content persistence -------------------------------------------------

    def _persist_review(self, parsed: dict, response_content: str) -> None:
        """Prepare proposals only. No review may mutate instructions or skills."""
        if not self._agent_dir:
            return
        from novacode_cli.hermes.ledger import LearningLedger

        ledger = LearningLedger(self._agent_dir / "learning.sqlite3")
        session = self._tracker.owner
        task_id = str(parsed.get("_task_id") or self._tracker.task)
        evidence = tuple(parsed.get("_evidence") or ())
        from novacode_cli.hermes.ledger import auto_approve_enabled

        auto = auto_approve_enabled()  # lessons and skills; never prompt changes
        for lesson in parsed.get("lessons", []):
            bullets = lesson.get("bullets") or []
            content = "\n".join(bullets) if isinstance(bullets, list) else str(bullets)
            if content.strip():
                ledger.propose(
                    content=content,
                    project=self._project or "",
                    session=session,
                    task=task_id or self._tracker.task,
                    applicability=str(lesson.get("topic") or ""),
                    evidence=evidence
                    or (f"review:{session}:outcome:{self._outcome[0]}:heuristic",),
                    auto_approve=auto,
                )
        for kind, value in (
            ("user_model", parsed.get("user_model")),
            ("habit", parsed.get("habits")),
        ):
            if value:
                ledger.propose(
                    content=str(value), scope="shared", kind=kind, session=session, origin="review"
                )
        from novacode_cli.hermes.skill_discovery import parse_skill_spec

        spec = parse_skill_spec(response_content)
        if spec:
            from novacode_cli.skills.schema import normalize_skill_frontmatter
            content = normalize_skill_frontmatter(spec["body"], spec["name"], spec["description"])
            ledger.propose(
                content=content, project=self._project or "", kind="skill", session=session,
                target=spec["name"], evidence=evidence, auto_approve=auto,
            )
        for block in re.findall(
            r"<prompt_improvement>(.*?)</prompt_improvement>", response_content, re.DOTALL
        ):
            ledger.propose(content=block.strip(), scope="shared", kind="lesson", session=session,
                           applicability="prompt improvement suggestion; requires a template proposal")

    async def _apply_review_content(
        self, response_content: str, *, task_id: str = "", evidence: tuple[str, ...] = ()
    ) -> None:
        """Store proposals and scoped metadata without any global promotion."""
        if not self._enabled:
            return
        import uuid

        from novacode_cli.hermes.memory_tiers import parse_review_response

        parsed = parse_review_response(response_content or "")
        parsed["_task_id"] = task_id
        parsed["_evidence"] = evidence
        await asyncio.to_thread(self._persist_review, parsed, response_content)
        review_id = uuid.uuid4().hex
        await self._store.aput(
            self._namespace("reviews"),
            review_id,
            {"timestamp": time.time(), "content": response_content[:2000]},
        )
        count = await self._get_review_count()
        await self._store.aput(
            self._namespace("meta"),
            "last_review",
            {"timestamp": time.time(), "review_count": count + 1},
        )

    def _namespace(self, name: str) -> tuple[str, ...]:
        return ("nova", self._tracker.owner, name)

    # -- Review state helpers ------------------------------------------------

    async def _get_review_count(self) -> int:
        """Return the total number of reviews completed from the durable store."""
        try:
            entry = await self._store.aget(self._namespace("meta"), "last_review")
            if entry and isinstance(entry.value, dict):
                return entry.value.get("review_count", 0)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to get review count")
        return 0

    async def _get_review_just_completed(self) -> bool:
        """Check if a review was just completed."""
        try:
            entry = await self._store.aget(self._namespace("meta"), "review_just_completed")
            if entry and isinstance(entry.value, dict):
                return entry.value.get("value", False)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to check if review just completed")
        return False

    async def _set_review_just_completed(self, value: bool) -> None:
        """Mark whether a review was just completed (to skip next trigger)."""
        if not self._enabled:
            return
        try:
            await self._store.aput(
                self._namespace("meta"),
                "review_just_completed",
                {"value": value},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to set review_just_completed flag")
