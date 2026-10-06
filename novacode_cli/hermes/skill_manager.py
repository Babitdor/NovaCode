"""Skill management — create, clean up, and refine skills from review feedback.

Extracted from ``NovaLearningMiddleware``.  Delegates to ``skill_discovery``
for the actual pattern-detection logic; this module owns the orchestration
decisions (when to create, guard against duplicates, rate-limit refinement).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.skill_manager")

# Minimum wall-clock gap between curation passes (curation is piggybacked on the
# review cycle but gated to roughly daily so cadence doesn't depend on chattiness).
_CURATION_INTERVAL_SECS = 86_400.0  # 24h


class SkillManager:
    """Manages skill lifecycle: creation from reviews, legacy cleanup, refinement.

    Used by ``ReviewRunner`` (via the middleware) after a review cycle completes.
    """

    def __init__(
        self,
        store: BaseStore,
        *,
        skills_dir: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._skills_dir = skills_dir
        self._enabled = enabled
        self._refinement_tasks: set[asyncio.Task] = set()

    # -- Background task management -----------------------------------------

    def spawn_task(self, coro) -> None:
        """Run a skill create/refine coroutine as a tracked background task."""
        task = asyncio.create_task(coro)
        self._refinement_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._refinement_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error("Skill task failed: %s", t.exception())

        task.add_done_callback(_done)

    @property
    def pending_tasks(self) -> set[asyncio.Task]:
        """Return the set of tracked background tasks."""
        return self._refinement_tasks

    def _log_refinement(self, action: str, target: str, detail: str = "") -> None:
        """Append a skill refinement event to the unified audit trail.

        Best-effort: a failed append never blocks the skill mutation. The skills
        dir lives at ``~/.nova/skills``, so the shared ``~/.nova`` root is its
        parent.
        """
        if not self._skills_dir:
            return
        try:
            from novacode_cli.hermes.refinement_log import append_refinement_event

            append_refinement_event(
                self._skills_dir.parent,
                domain="skill",
                action=action,
                target=target,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not append skill refinement event", exc_info=True)

    # -- Skill creation from review -----------------------------------------

    async def maybe_create_from_review(self, review_text: str) -> None:
        """Persist an episode-grounded skill if the review proposed one.

        The review LLM writes a ``<skill>`` block (name + trigger + steps) only
        when it recognizes a genuinely reusable workflow from this session.
        Deduped by skill name (directory existence + store record).
        """
        if not self._enabled or not self._skills_dir:
            return
        try:
            from novacode_cli.hermes.skill_discovery import (
                parse_skill_spec,
                write_skill_from_spec,
            )

            spec = parse_skill_spec(review_text)
            if spec is None:
                return
            await write_skill_from_spec(spec, self._skills_dir, self._store)
            self._log_refinement("skill", "create", spec.get("name") or "unknown")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to create skill from review")

    # -- Legacy cleanup -----------------------------------------------------

    async def cleanup_legacy_skills_once(self) -> None:
        """One-time removal of old ``nova-<hash>`` n-gram skills (guarded)."""
        if not self._enabled or not self._skills_dir:
            return
        try:
            done = await self._store.aget(("nova", "meta"), "legacy_skills_cleaned_v1")
            if done is not None:
                return
            from novacode_cli.hermes.skill_discovery import (
                cleanup_legacy_pattern_skills,
            )

            removed = cleanup_legacy_pattern_skills(self._skills_dir)
            await self._store.aput(
                ("nova", "meta"), "legacy_skills_cleaned_v1", {"removed": removed}
            )
            if removed:
                from novacode_cli.events import nova_event_log

                nova_event_log.append(
                    (
                        "nova_skill_refinement",
                        "◐",
                        "yellow",
                        f"▤ Nova: removed {len(removed)} legacy auto-skill(s) "
                        "(opaque nova-* patterns; replaced by episode-grounded skills)",
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed legacy skill cleanup")

    # -- Skill refinement ---------------------------------------------------

    async def maybe_refine_skills(self) -> None:
        """Refine one high-failure skill per cycle, grounded in its failures.

        ``check_skill_effectiveness`` only surfaces ``high_failure`` candidates
        (with a cooldown + attempt cap baked in), so a skill can't be re-refined
        on every cycle. Chronically-unused skills are handled by the curator
        (archival), not by rewriting — see ``check_skill_effectiveness``.
        """
        if not self._enabled or not self._skills_dir:
            return
        try:
            from novacode_cli.hermes.skill_discovery import (
                check_skill_effectiveness,
                refine_skill,
            )

            candidates = await check_skill_effectiveness(self._store)
            for skill_name, issue in candidates:
                if issue != "high_failure":
                    continue
                if not (self._skills_dir / skill_name / "SKILL.md").exists():
                    continue

                # Pull captured failure samples so the refinement is grounded in
                # what actually went wrong, not a blind regenerate.
                failure_samples: list = []
                try:
                    entry = await self._store.aget(("nova", "skill_usage"), skill_name)
                    if entry and isinstance(entry.value, dict):
                        failure_samples = list(entry.value.get("failure_samples") or [])
                except Exception:  # noqa: BLE001
                    failure_samples = []

                from novacode_cli.events import nova_event_log

                nova_event_log.append(
                    (
                        "nova_skill_refinement",
                        "◐",
                        "yellow",
                        f"◐ Nova: skill '{skill_name}' needs refinement ({issue})",
                    )
                )
                self.spawn_task(
                    refine_skill(
                        skill_name,
                        self._skills_dir,
                        issue,
                        failure_samples=failure_samples,
                        store=self._store,
                    )
                )
                self._log_refinement("refine", skill_name, f"high_failure: {issue}")
                break  # one refinement per cycle
        except Exception:  # noqa: BLE001
            logger.exception("Failed to check skill effectiveness / refine")

    # -- Skill curation -----------------------------------------------------

    async def maybe_curate(self) -> None:
        """Run a background curation pass on a real schedule (≥ once/24h).

        Called opportunistically (every Nth review), but a time gate makes the
        cadence wall-clock-based, not review-count-based — so curation runs about
        daily regardless of how chatty a session is.
        """
        if not self._enabled or not self._skills_dir:
            return
        try:
            import time

            now = time.time()
            last = 0.0
            try:
                rec = await self._store.aget(("nova", "meta"), "last_curation_ts")
                if rec and isinstance(rec.value, dict):
                    last = float(rec.value.get("ts", 0.0))
            except Exception:  # noqa: BLE001
                last = 0.0
            if now - last < _CURATION_INTERVAL_SECS:
                return

            # Stamp before spawning so a slow pass can't trigger a duplicate.
            await self._store.aput(("nova", "meta"), "last_curation_ts", {"ts": now})

            from novacode_cli.hermes.curator import run_curation

            self.spawn_task(run_curation(self._store, self._skills_dir))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to start skill curation")
