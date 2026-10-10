"""Skill management — create, clean up, and refine skills from review feedback.

Extracted from ``NovaLearningMiddleware``.  Delegates to ``skill_discovery``
for the actual pattern-detection logic; this module owns the orchestration
decisions (when to create, guard against duplicates, rate-limit refinement).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.skill_manager")

# Minimum wall-clock gap between curation passes (curation is piggybacked on the
# review cycle but gated to roughly daily so cadence doesn't depend on chattiness).
_CURATION_INTERVAL_SECS = 86_400.0  # 24h


# Skill proposals seen so far, by project (see SkillManager._proposed_elsewhere).
_PROPOSALS_FILE = ".skill-proposals.json"

# `<scope>general</scope>` inside a review's `<skill>` block.
_GENERAL_SKILL_RE = re.compile(
    r"<skill>.*?<scope>\s*general\s*</scope>.*?</skill>", re.DOTALL | re.IGNORECASE
)


def _similar_skill(skills_dir: Path, name: str, overlap: float = 0.6) -> str | None:
    """An existing skill whose name is mostly the same words as ``name``, if any.

    An exact match is not reported: writing over a skill of the same name is the
    existing update path and stays as it was.
    """
    words = {w for w in re.split(r"[-_\s]+", name.lower()) if w}
    if not words or not skills_dir.is_dir():
        return None
    for path in skills_dir.iterdir():
        if not path.is_dir() or path.name == name:
            continue
        other = {w for w in re.split(r"[-_\s]+", path.name.lower()) if w}
        if other and len(words & other) / len(words | other) >= overlap:
            return path.name
    return None


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
        project: str | None = None,
    ) -> None:
        self._store = store
        self._project = project or ""
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
            # Skills live in one directory shared by every project. A workflow
            # that only fits a throwaway sandbox would be offered everywhere
            # afterwards and useful nowhere: on Terminal-Bench the loop saved
            # `build-rust-cpp-polyglot` — one task's answer — as a skill. Keep
            # only what the review marked as general.
            if self._project.startswith("sandbox-"):
                if not _GENERAL_SKILL_RE.search(review_text):
                    self._log_refinement("skill", "skip", f"{spec.get('name')} (sandbox-specific)")
                    return
                # "General" is the review's opinion, and it is generous with it
                # (it called `write-arithmetic-coder-encoder` general). Wait for
                # a second, different sandbox to propose the same kind of skill.
                if not self._proposed_elsewhere(spec):
                    self._log_refinement("skill", "hold", f"{spec.get('name')} (seen in one project)")
                    return
            # Reviews name skills freely, so one workflow came back as
            # `setup-python-torch-container` and `setup-python-torch-env`.
            # Exact-name dedup does not catch that; word overlap does.
            twin = _similar_skill(self._skills_dir, spec.get("name") or "")
            if twin:
                self._log_refinement("skill", "skip", f"{spec.get('name')} (same as {twin})")
                return
            await write_skill_from_spec(spec, self._skills_dir, self._store)
            self._log_refinement("skill", "create", spec.get("name") or "unknown")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to create skill from review")

    def _proposed_elsewhere(self, spec: dict) -> bool:
        """Has a different project already proposed this kind of skill?

        Proposals are remembered in a small ledger beside the skills. A match is
        a similar name or a description stating the same thing; the first
        sighting is recorded and answers False.
        """
        from novacode_cli.hermes.memory_tiers import _fact_words, _same_fact

        name = str(spec.get("name") or "")
        described = _fact_words(str(spec.get("description") or ""))
        named = {w for w in re.split(r"[-_\s]+", name.lower()) if w}
        ledger_path = self._skills_dir / _PROPOSALS_FILE
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = {}
        for other_name, entry in ledger.items():
            if entry.get("project") == self._project:
                continue
            other_named = {w for w in re.split(r"[-_\s]+", other_name.lower()) if w}
            same_name = named and len(named & other_named) / len(named | other_named) >= 0.6
            if same_name or _same_fact(described, frozenset(entry.get("words") or ())):
                return True
        # One entry per idea per project: a long task re-proposes the same skill
        # under a new name at every review (five "raw tensor dump" variants from
        # one task), and none of those is a second opinion.
        for other_name, entry in ledger.items():
            if entry.get("project") != self._project:
                continue
            other_named = {w for w in re.split(r"[-_\s]+", other_name.lower()) if w}
            # Looser than the cross-project test (share of the SHORTER name):
            # `inspect-raw-tensor-dump` and `reverse-engineer-raw-tensor-dump`
            # from one task are plainly the same proposal.
            same_name = (
                named
                and other_named
                and len(named & other_named) / min(len(named), len(other_named)) >= 0.6
            )
            if same_name or _same_fact(described, frozenset(entry.get("words") or ())):
                return False  # already holding this one
        ledger[name] = {"project": self._project, "words": sorted(described)}
        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
        except OSError:
            logger.debug("Could not record skill proposal", exc_info=True)
        return False

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
