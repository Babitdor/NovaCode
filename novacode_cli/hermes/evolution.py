"""Self-Evolution engine — complex-task completion unlocks a new skill.

The Procedural tier's *deliberate* skill-creation pathway. Where ``ReviewRunner``
mints skills opportunistically on tool-count/failure signals, the evolution
engine fires at **task completion** (the main agent's ``aafter_agent`` hook):

    1. Slice the task's episodic window (``query_episodes`` since task start).
    2. Score its complexity (cheap heuristic) — a trivial chat scores low and
       costs nothing.
    3. If complex enough, run an out-of-band model call that returns *exactly
       one* of:
         - ``<skill>``   → a brand-new skill (🧬 unlocked), or
         - ``<level_up>``→ a targeted improvement to a matching existing skill
           (⬆️ levelled up),
       grounded in the current skill library so the choice is create-or-level-up.
    4. Persist to the evolution log + emit a TUI event.

Like the review engine, this runs fire-and-forget OOB (never injected into the
agent's task turn) and degrades gracefully — a failure here must never break the
turn. It only ever fires for the **main** agent: subagents don't inherit this
middleware (see ``_harden_subagent_specs`` in ``agents/core_agent.py``).
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import SystemMessage

from novacode_cli.events import cap_event_log, nova_event_log
from novacode_cli.hermes.review import (
    _SUBSTANTIVE_TOOLS,
    _TRIVIAL_BUILTINS,
    _window_recovered,
)
from novacode_cli.prompts import render_template

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.store.base import BaseStore

    from novacode_cli.hermes.skill_manager import SkillManager
    from novacode_cli.hermes.tracker import ToolUsageTracker

logger = logging.getLogger("nova.hermes.evolution")

# ── Complexity scoring weights (tunable) ────────────────────────────────────
_W_SUBSTANTIVE = 2  # per write/edit/execute/test or non-builtin tool call
_W_DISTINCT = 1  # per distinct tool used
_W_SUBAGENT = 3  # per `task` (subagent) dispatch
_W_TODO = 2  # per completed todo
_W_RECOVERY = 5  # one-off bonus for an error that was then recovered from
# A task must score at least this to be "complex enough" to evolve.
COMPLEXITY_THRESHOLD = 8

# How many trailing thread messages to feed the OOB evolution call.
_MAX_CONTEXT_MESSAGES = 50

# <level_up skill="name"> … <body>…</body> … </level_up>
_LEVEL_UP_RE = re.compile(
    r'<level_up\s+skill\s*=\s*["\']?([^"\'>]+)["\']?\s*>(.*?)</level_up>',
    re.DOTALL | re.IGNORECASE,
)
_BODY_RE = re.compile(r"<body>(.*?)</body>", re.DOTALL | re.IGNORECASE)


def _emit(event_type: str, message: str) -> None:
    """Append an evolution event to the shared Nova buffer (TUI-safe)."""
    icons = {
        "nova_skill_unlocked": ("✦", "green"),
        "nova_skill_leveled": ("↑", "cyan"),
        "nova_evolution_gate": ("✦", "dim"),
    }
    icon, color = icons.get(event_type, ("•", "cyan"))
    try:
        nova_event_log.append((event_type, icon, color, message))
        cap_event_log()
    except Exception:  # noqa: BLE001
        logger.debug("evolution event (not surfaced): %s — %s", event_type, message)


def score_task_complexity(
    window: list[dict[str, Any]],
    state: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    """Score a completed task's complexity from its episodic window + state.

    Returns ``(score, breakdown)``. ``score >= COMPLEXITY_THRESHOLD`` means the
    task is worth evolving from. Pure/deterministic — no LLM, no I/O.
    """
    tools = [e.get("tool") for e in window]
    # A `task` dispatch is counted under subagents, not substantive, to avoid
    # double-weighting it.
    substantive = sum(
        1 for t in tools if t in _SUBSTANTIVE_TOOLS or (t not in _TRIVIAL_BUILTINS and t != "task")
    )
    distinct = len(set(tools))
    subagents = sum(1 for t in tools if t == "task")

    todos = (state or {}).get("todos") or []
    todos_done = sum(1 for td in todos if isinstance(td, dict) and td.get("status") == "completed")
    recovered = _window_recovered(window)

    score = (
        substantive * _W_SUBSTANTIVE
        + distinct * _W_DISTINCT
        + subagents * _W_SUBAGENT
        + todos_done * _W_TODO
        + (_W_RECOVERY if recovered else 0)
    )
    breakdown = {
        "substantive": substantive,
        "distinct_tools": distinct,
        "subagents": subagents,
        "todos_completed": todos_done,
        "recovered": recovered,
        "score": score,
    }
    return score, breakdown


def _task_summary(messages: list[Any]) -> str:
    """Best-effort one-line summary of the task (the last human request)."""
    for msg in reversed(messages or []):
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if role in ("human", "user"):
            content = getattr(msg, "content", "")
            text = content if isinstance(content, str) else str(content)
            text = " ".join(text.split())
            if text:
                return text[:160]
    return ""


def parse_level_up(text: str) -> dict[str, str] | None:
    """Extract a ``<level_up skill="name"><body>…</body></level_up>`` block.

    Returns ``{"skill", "body"}`` (the complete improved SKILL.md), or ``None``.
    """
    match = _LEVEL_UP_RE.search(text or "")
    if not match:
        return None
    skill = match.group(1).strip()
    body_match = _BODY_RE.search(match.group(2))
    body = (body_match.group(1) if body_match else match.group(2)).strip()
    if not skill or not body:
        return None
    return {"skill": skill, "body": body}


class EvolutionEngine:
    """Task-completion skill evolution: score, then create-or-level-up.

    Created by ``NovaLearningMiddleware`` with the shared ``ToolUsageTracker``
    and ``SkillManager`` injected (mirrors how ``ReviewRunner`` is wired).
    """

    def __init__(
        self,
        store: BaseStore,
        tracker: ToolUsageTracker,
        skill_manager: SkillManager,
        *,
        skills_dir: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._skill_manager = skill_manager
        self._skills_dir = skills_dir
        self._enabled = enabled

    # -- Gate ---------------------------------------------------------------

    async def maybe_evolve(
        self,
        state: dict[str, Any],
        task_start_ts: float | None,
    ) -> None:
        """Score the just-completed task; if complex, spawn an OOB evolution.

        Never raises — a learning failure must never break the turn.
        """
        if not self._enabled or not self._skills_dir:
            return
        try:
            since = task_start_ts if task_start_ts and task_start_ts > 0 else None
            window = await self._tracker.query_episodes(limit=500, since=since)
            if not window:
                return
            score, breakdown = score_task_complexity(window, state)
            if score < COMPLEXITY_THRESHOLD:
                return

            _emit(
                "nova_evolution_gate",
                f"✦ Complex task complete (score {score}) — evolving…",
            )
            messages = list((state or {}).get("messages") or [])[-_MAX_CONTEXT_MESSAGES:]
            self._skill_manager.spawn_task(self.run_evolution(messages, breakdown))
        except Exception:  # noqa: BLE001
            logger.exception("Self-evolution gate failed")

    # -- OOB consolidation --------------------------------------------------

    async def run_evolution(
        self,
        messages: list[Any],
        breakdown: dict[str, Any],
    ) -> None:
        """Run the create-or-level-up consolidation as a separate model call."""
        if not self._enabled or not self._skills_dir:
            return
        try:
            library = self._existing_skills()
            prompt = render_template(
                "nova_evolution.jinja",
                complexity=breakdown,
                skill_library=library,
            )
            from novacode_cli.config.model_create import create_model

            model = create_model()
            convo = [*messages, SystemMessage(content=prompt)]
            resp = await model.ainvoke(
                convo,
                config={
                    "run_name": "nova_evolution",
                    "tags": ["nova", "hermes", "evolution"],
                    # nova_oob: out-of-band marker so the agent loop drops this
                    # call's streamed output (see agent_loop's nova_oob filter).
                    "metadata": {
                        "nova_oob": True,
                        "nova_complexity": breakdown.get("score", 0),
                    },
                },
            )
            raw = getattr(resp, "content", "")
            content = raw if isinstance(raw, str) else str(raw)

            await self._apply_evolution(content, messages, breakdown)
        except Exception:  # noqa: BLE001
            logger.exception("Self-evolution consolidation failed")

    def _existing_skills(self) -> list[dict[str, str]]:
        """Return ``[{"name","description"}]`` for the user skill library."""
        try:
            from novacode_cli.hermes.curator import _scan_skills

            scanned = _scan_skills(self._skills_dir)  # type: ignore[arg-type]
            return [
                {"name": name, "description": desc}
                for name, (_dir, desc) in sorted(scanned.items())
            ]
        except Exception:  # noqa: BLE001
            logger.debug("Could not scan skill library", exc_info=True)
            return []

    async def _apply_evolution(
        self,
        content: str,
        messages: list[Any],
        breakdown: dict[str, Any],
    ) -> None:
        """Write a new skill or level up an existing one, then record it."""
        from novacode_cli.hermes.skill_discovery import (
            parse_skill_spec,
            write_skill_from_spec,
        )

        # Prefer a level-up when the LLM matched an existing skill on disk.
        level = parse_level_up(content)
        if level and self._skills_dir:
            skill_dir = self._skills_dir / level["skill"]
            if (skill_dir / "SKILL.md").exists():
                if await self._level_up(skill_dir, level["body"]):
                    await self._record("levelup", level["skill"], messages, breakdown)
                return

        spec = parse_skill_spec(content)
        if spec:
            # Adversarial debate: one critic round before freezing the skill.
            # Rejects vague/duplicate skills; may return a revised spec. Degrades
            # to approving the draft on any failure (NOVA_SKILL_DEBATE=0 disables).
            from novacode_cli.hermes.skill_debate import debate_skill_spec

            revised, verdict = await debate_skill_spec(
                spec, skill_library=self._existing_skills()
            )
            await self._record_debate(spec.get("name", ""), verdict)
            if revised is None:
                return  # critic rejected — do not freeze
            name = await write_skill_from_spec(revised, self._skills_dir, self._store)
            if name:
                _emit("nova_skill_unlocked", f"✦ New skill unlocked: {name}")
                await self._record("unlock", name, messages, breakdown)

    async def _level_up(self, skill_dir: Path, new_body: str) -> bool:
        """Snapshot then overwrite a skill's SKILL.md (rollback-safe)."""
        skill_path = skill_dir / "SKILL.md"
        try:
            current = skill_path.read_text(encoding="utf-8")
        except OSError:
            return False
        new_md = new_body.strip()
        if not new_md or new_md == current.strip():
            return False
        try:
            from novacode_cli.skills import versioning

            versioning.snapshot(skill_dir, reason="level_up", source="evolution")
            skill_path.write_text(new_md.rstrip() + "\n", encoding="utf-8")
        except OSError:
            logger.exception("Failed to write levelled-up skill '%s'", skill_dir.name)
            return False
        _emit("nova_skill_leveled", f"↑ Skill levelled up: {skill_dir.name}")
        return True

    # -- Persistence --------------------------------------------------------

    async def _record_debate(self, skill: str, verdict: str) -> None:
        """Append a skill-debate outcome to its log (best-effort, auditable)."""
        try:
            ts = time.time()
            await self._store.aput(
                ("nova", "skill_debate"),
                f"dbt_{int(ts * 1000)}",
                {"ts": ts, "skill": skill, "verdict": verdict},
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not write skill_debate log", exc_info=True)

    async def _record(
        self,
        kind: str,
        skill: str,
        messages: list[Any],
        breakdown: dict[str, Any],
    ) -> None:
        """Append an evolution-log entry and bump the meta counters."""
        ts = time.time()
        try:
            await self._store.aput(
                ("nova", "evolution_log"),
                f"evo_{int(ts * 1000)}",
                {
                    "ts": ts,
                    "kind": kind,
                    "skill": skill,
                    "task_summary": _task_summary(messages),
                    "score": breakdown.get("score", 0),
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not write evolution_log", exc_info=True)

        try:
            entry = await self._store.aget(("nova", "meta"), "evolution")
            counters = (
                dict(entry.value)
                if entry and isinstance(entry.value, dict)
                else {"unlocked": 0, "leveled": 0}
            )
            key = "unlocked" if kind == "unlock" else "leveled"
            counters[key] = int(counters.get(key, 0)) + 1
            await self._store.aput(("nova", "meta"), "evolution", counters)
        except Exception:  # noqa: BLE001
            logger.debug("Could not bump evolution counters", exc_info=True)
