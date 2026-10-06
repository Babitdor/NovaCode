"""The ``/refine`` self-refinement loop — plan → apply → review → rollback.

Inspired by Prime Agent's ``/refine`` (see ``docs/PRIME-AGENT-LEARNING-ANALYSIS.md``
and ``docs/REFINE-COMMAND-DESIGN.md``). Where the automatic Hermes paths refine
skills on a single trigger (``high_failure``), this is the **explicit, on-demand**
loop: it reads the agent's recent trajectory + current learning state, proposes
the *smallest* evidence-backed changes across the harness domains, applies them
through the existing versioned write paths, runs a review gate on each, and rolls
back anything that fails.

Domains supported today:
    - ``skill``  → ``skill_discovery.refine_skill`` (snapshot + cooldown + audit)
    - ``prompt`` → ``PromptEvolutionEngine._write_candidate`` (candidate override,
                   never touches the packaged template)
    - ``memory`` → ``memory_tiers.record_lesson`` (append-only, deduped, compacted;
                   reviewed BEFORE applying because there is no rollback)

``subagent`` (code, not user state) is deliberately out of scope — see the design
doc's scope guard.

The loop is best-effort and never raises: a failure at any step is logged and the
run degrades gracefully. It never blocks the agent turn.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from langchain_core.messages import SystemMessage

from novacode_cli.prompts import render_template

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.store.base import BaseStore

    from novacode_cli.hermes.tracker import ToolUsageTracker

logger = logging.getLogger("nova.hermes.refine_loop")

#: Max plan items per run (one per domain max) — keeps a run surgical.
_MAX_PLAN_ITEMS = 3
#: Max chars of trajectory evidence fed to the planner (context bound).
_MAX_EVIDENCE_CHARS = 4000

# <refine_plan> … </refine_plan> with <item> children.
_PLAN_RE = re.compile(r"<refine_plan>(.*?)</refine_plan>", re.DOTALL | re.IGNORECASE)
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL | re.IGNORECASE)
_FIELD_RE = re.compile(
    r"<(?P<key>domain|target|action|reason|change)>(?P<val>.*?)</(?P=key)>",
    re.DOTALL | re.IGNORECASE,
)
# <verdict>accept|reject</verdict>
_VERDICT_RE = re.compile(r"<verdict>\s*(accept|reject)\s*</verdict>", re.IGNORECASE)
_REASON_RE = re.compile(r"<reason>(.*?)</reason>", re.DOTALL | re.IGNORECASE)

#: Domains the loop can currently apply + roll back.
_SUPPORTED_DOMAINS = frozenset({"skill", "prompt", "memory"})
#: Actions the planner may emit. ``delete`` may omit ``change``; the rest require it.
_VALID_ACTIONS = frozenset({"patch", "update", "create", "delete"})
#: Domains whose edits are append-only (no rollback) — reviewed BEFORE applying.
_REVIEW_FIRST_DOMAINS = frozenset({"memory"})


def _emit_tui_event(event_type: str, message: str) -> None:
    """Surface a refine-loop notification via the shared Nova event buffer.

    The loop runs from a slash command (not inside the agent loop), but it may
    also be invoked from Hermes, so we route through ``nova_event_log`` to stay
    TUI-safe. Best-effort — never raise from a notification.
    """
    try:
        from novacode_cli.events import nova_event_log

        nova_event_log.append((event_type, "◐", "cyan", message))
    except Exception:  # noqa: BLE001
        logger.debug("refine event (not surfaced): %s", message)


def _nova_root() -> Path:
    """Resolve the shared ``~/.nova`` root (parent of prompt_history)."""
    from novacode_cli.hermes.prompt_evolution import PROMPT_HISTORY_DIR

    return PROMPT_HISTORY_DIR.parent


def _user_skills_dir() -> Path:
    """Return the user skills dir (created if missing)."""
    from novacode_cli.config.config import Settings

    return Settings.from_environment().ensure_user_skills_dir()


def _agent_dir() -> Path:
    """Return the default agent dir (``~/.nova/agents/nova-agent``)."""
    from novacode_cli.config.config import MAIN_AGENT_ID, Settings

    return Settings.from_environment().get_agent_dir(MAIN_AGENT_ID)


def _parse_plan(text: str) -> list[dict[str, str]]:
    """Parse a ``<refine_plan>`` response into a list of item dicts.

    Returns only well-formed items with a supported domain. Best-effort: a
    malformed item is skipped, never fatal.
    """
    items: list[dict[str, str]] = []
    plan_m = _PLAN_RE.search(text)
    body = plan_m.group(1) if plan_m else text
    for item_m in _ITEM_RE.finditer(body):
        fields: dict[str, str] = {}
        for fm in _FIELD_RE.finditer(item_m.group(1)):
            fields[fm.group("key")] = fm.group("val").strip()
        domain = fields.get("domain", "").strip().lower()
        if domain not in _SUPPORTED_DOMAINS:
            continue
        action = fields.get("action", "").strip().lower()
        if action not in _VALID_ACTIONS:
            continue
        if not fields.get("target"):
            continue
        # create/update/patch must carry a change body; delete may omit it.
        if action != "delete" and not fields.get("change", "").strip():
            continue
        items.append(fields)
        if len(items) >= _MAX_PLAN_ITEMS:
            break
    return items


def _parse_verdict(text: str) -> tuple[bool, str]:
    """Parse a review-gate response into ``(accepted, reason)``."""
    m = _VERDICT_RE.search(text)
    if not m:
        return False, "no <verdict> block in review response"
    accepted = m.group(1).strip().lower() == "accept"
    reason_m = _REASON_RE.search(text)
    reason = reason_m.group(1).strip() if reason_m else ""
    return accepted, reason


async def _gather_evidence(store: BaseStore, tracker: ToolUsageTracker) -> str:
    """Assemble a compact trajectory + skill-effectiveness evidence digest.

    Best-effort: any unreadable source is omitted. The digest is what grounds
    the planner's proposals, so it must be evidence, not speculation.
    """
    parts: list[str] = []

    # Recent tool history (last ~20 calls) — the raw trajectory.
    try:
        history = await tracker.get_tool_history(limit=20)
        if history:
            lines = [f"- [{h.get('tool', '?')}] success={h.get('success', True)}" for h in history]
            parts.append("Recent tool calls:\n" + "\n".join(lines))
    except Exception:  # noqa: BLE001
        logger.debug("refine: could not read tool history", exc_info=True)

    # Skill effectiveness — high-failure candidates worth refining.
    try:
        from novacode_cli.hermes.skill_discovery import check_skill_effectiveness

        candidates = await check_skill_effectiveness(store)
        if candidates:
            parts.append(
                "Skills flagged for refinement: "
                + ", ".join(f"{name} ({issue})" for name, issue in candidates)
            )
    except Exception:  # noqa: BLE001
        logger.debug("refine: could not check skill effectiveness", exc_info=True)

    # Per-skill usage stats (invocations / successes / failures).
    try:
        results = await store.asearch(("nova", "skill_usage"))
        usage_lines = []
        for item in results or []:
            if not hasattr(item, "key") or not isinstance(getattr(item, "value", None), dict):
                continue
            v = item.value
            usage_lines.append(
                f"- {item.key}: invocations={v.get('invocations', 0)} "
                f"successes={v.get('successes', 0)} failures={v.get('failures', 0)}"
            )
        if usage_lines:
            parts.append("Skill usage:\n" + "\n".join(usage_lines))
    except Exception:  # noqa: BLE001
        logger.debug("refine: could not read skill usage", exc_info=True)

    digest = "\n\n".join(parts)
    if len(digest) > _MAX_EVIDENCE_CHARS:
        digest = digest[-_MAX_EVIDENCE_CHARS:]
    return digest or "(no trajectory evidence available)"


def _list_skills(skills_dir: Path) -> list[dict[str, str]]:
    """List user/project skills as ``{name, description}`` (frontmatter desc)."""
    out: list[dict[str, str]] = []
    if not skills_dir.is_dir():
        return out
    try:
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if not child.is_dir() or not skill_md.exists():
                continue
            desc = ""
            try:
                text = skill_md.read_text(encoding="utf-8")
                m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
                if m:
                    desc = m.group(1).strip().strip('"').strip("'")
            except OSError:
                logger.debug("refine: could not read skill %s", child.name, exc_info=True)
            out.append({"name": child.name, "description": desc})
    except OSError:
        logger.debug("refine: could not list skills under %s", skills_dir, exc_info=True)
    return out


def _prompt_state() -> list[dict[str, object]]:
    """Return per-template override state (active/candidate flags)."""
    from novacode_cli.hermes.prompt_evolution import PromptEvolutionEngine

    try:
        return PromptEvolutionEngine().status()
    except Exception:  # noqa: BLE001
        logger.debug("refine: could not read prompt state", exc_info=True)
        return []


def _memory_topics() -> list[str]:
    """Return memory topic names (files under ``memories/`` minus INDEX)."""
    try:
        memories_dir = _agent_dir() / "memories"
        if not memories_dir.is_dir():
            return []
        return sorted(
            p.stem
            for p in memories_dir.glob("*.md")
            if p.stem.lower() not in ("index",)
        )
    except OSError:
        logger.debug("refine: could not list memory topics", exc_info=True)
        return []


async def _apply_skill(item: dict[str, str], store: BaseStore) -> bool:
    """Apply a skill refinement via the versioned write path.

    The planner's ``<change>`` is the full improved SKILL.md body. We snapshot
    the prior version, write the new body, and record a cooldown — reusing the
    same machinery as ``refine_skill`` but without a second model call (the
    planner already produced the content).
    """
    from novacode_cli.skills import versioning

    skills_dir = _user_skills_dir()
    target = item["target"]
    skill_dir = skills_dir / target
    skill_path = skill_dir / "SKILL.md"
    if not skill_path.exists():
        _emit_tui_event("nova_skill_error", f"Cannot refine '{target}': SKILL.md not found")
        return False

    new_md = item.get("change", "").strip()
    if not new_md:
        return False
    # Ensure YAML frontmatter is present (the planner may omit it).
    if not new_md.startswith("---"):
        from novacode_cli.skills.skill_creation import _add_frontmatter

        new_md = _add_frontmatter(new_md, target, None)

    try:
        current = skill_path.read_text(encoding="utf-8")
        if new_md.strip() == current.strip():
            return False
        versioning.snapshot(skill_dir, reason="refine_loop", source="refine")
        skill_path.write_text(new_md.rstrip() + "\n", encoding="utf-8")
    except OSError:
        return False
    # Record a cooldown so the same skill isn't re-refined every run.
    await _record_cooldown(store, target)
    return True


async def _record_cooldown(store: BaseStore, target: str) -> None:
    """Best-effort cooldown record so a skill isn't re-refined every run."""
    try:
        from novacode_cli.hermes.skill_discovery import _record_refinement

        await _record_refinement(store, target, "refine_loop")
    except Exception:  # noqa: BLE001
        logger.debug("Could not record refine cooldown for '%s'", target, exc_info=True)


async def _apply_prompt(item: dict[str, str]) -> bool:
    """Apply a prompt override via the prompt-evolution candidate write path.

    Writes a candidate override under ``~/.nova/prompt_history/<stem>/`` — the
    packaged template is never touched, so ``/prompt rollback`` can always revert.
    """
    from novacode_cli.hermes.prompt_evolution import PromptEvolutionEngine

    engine = PromptEvolutionEngine()
    name = engine._normalise_name(item["target"])
    if name is None:
        _emit_tui_event("nova_skill_error", f"Cannot refine: unknown template '{item['target']}'")
        return False
    body = item.get("change", "")
    if not body.strip():
        return False
    await engine._write_candidate(name, body)
    return True


async def _apply_memory(item: dict[str, str]) -> bool:
    """Apply a memory refinement via the append-only lesson write path.

    The planner's ``<change>`` is a bullet list (one per line) recorded under the
    target topic. Memory is append-only by design — there is no rollback, so
    ``run_refine`` reviews memory edits BEFORE applying them (see
    ``_REVIEW_FIRST_DOMAINS``).
    """
    from novacode_cli.hermes.memory_tiers import record_lesson

    topic = item["target"]
    bullets = item.get("change", "").strip()
    if not bullets:
        return False
    try:
        record_lesson(_agent_dir(), topic, bullets)
    except Exception:  # noqa: BLE001
        logger.exception("Refine memory write failed for '%s'", topic)
        return False
    return True


async def _apply(item: dict[str, str], store: BaseStore) -> bool:
    """Apply one plan item through its domain's versioned write path."""
    domain = item["domain"]
    if domain == "skill":
        return await _apply_skill(item, store)
    if domain == "prompt":
        return await _apply_prompt(item)
    if domain == "memory":
        return await _apply_memory(item)
    return False


async def _review(item: dict[str, str], evidence: str) -> tuple[bool, str]:
    """Run the review gate on one applied change.

    Returns ``(accepted, reason)``. On any failure (model error, no verdict) the
    change is treated as rejected so it gets rolled back — fail-safe.
    """
    try:
        from novacode_cli.config.model_create import create_model

        prompt = render_template(
            "refine_review.jinja",
            domain=item.get("domain", "?"),
            target=item.get("target", "?"),
            action=item.get("action", "?"),
            reason=item.get("reason", ""),
            change=item.get("change", ""),
            evidence=evidence,
        )
        model = create_model()
        resp = await model.ainvoke(
            [SystemMessage(content=prompt)],
            config={
                "run_name": "nova_oob_refine_review",
                "tags": ["nova", "hermes", "refine-review"],
                "metadata": {"nova_oob": True, "refine_target": item.get("target")},
            },
        )
        raw = getattr(resp, "content", "")
        text = raw if isinstance(raw, str) else str(raw)
        return _parse_verdict(text)
    except Exception as exc:
        logger.exception("Refine review gate failed for '%s'", item.get("target"))
        return False, f"review gate error: {exc}"


async def _rollback(item: dict[str, str]) -> None:
    """Roll back a failed change via the domain's existing rollback path."""
    domain = item["domain"]
    target = item["target"]
    try:
        if domain == "skill":
            from novacode_cli.skills import versioning

            skill_dir = _user_skills_dir() / target
            if (skill_dir / "SKILL.md").exists():
                versioning.restore(skill_dir)
        elif domain == "prompt":
            from novacode_cli.hermes.prompt_evolution import PromptEvolutionEngine

            engine = PromptEvolutionEngine()
            name = engine._normalise_name(target)
            if name is not None:
                await engine.rollback(name)
        elif domain == "memory":
            # Memory is append-only by design — nothing to restore. The review
            # gate runs BEFORE applying memory edits, so a rejected edit never
            # reaches the file in the first place.
            logger.debug("refine: memory rollback is a no-op (append-only)")
    except Exception:
        logger.exception("Refine rollback failed for '%s'", target)


def _read_current(item: dict[str, str]) -> str:
    """Best-effort read of the current content for the item's target.

    Used for the audit log's ``before`` snapshot. Returns ``""`` when the target
    has no readable current content (e.g. a prompt with no active override).
    """
    domain = item["domain"]
    target = item["target"]
    try:
        if domain == "skill":
            p = _user_skills_dir() / target / "SKILL.md"
            return p.read_text(encoding="utf-8") if p.exists() else ""
        if domain == "memory":
            p = _agent_dir() / "memories" / f"{target}.md"
            return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        logger.debug("refine: could not snapshot '%s' before edit", target, exc_info=True)
    return ""


def _log_event(
    domain: str,
    action: str,
    target: str,
    outcome: str,
    detail: str = "",
    before: str = "",
    after: str = "",
) -> None:
    """Append a refine event to the unified audit trail (best-effort)."""
    try:
        from novacode_cli.hermes.refinement_log import append_refinement_event

        append_refinement_event(
            _nova_root(),
            domain=domain,
            action=action,
            target=target,
            detail=detail,
            outcome=outcome,
            before=before,
            after=after,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not append refine event", exc_info=True)


async def should_refine(store: BaseStore, tracker: ToolUsageTracker) -> tuple[bool, str]:
    """Cheap pre-plan review gate: is this checkpoint worth a /refine run at all?

    Runs a lightweight model call over the trajectory digest and current harness
    state, returning ``(should_refine, rationale)``. Rejects one-off noise,
    unsupported hypotheses, and transient tool outputs before the expensive plan
    call is spent. Best-effort: on any failure the gate defaults to ``True`` so
    the loop still runs (fail-open — the plan + review gates remain the real
    quality barrier).
    """
    try:
        from novacode_cli.config.model_create import create_model

        evidence = await _gather_evidence(store, tracker)
        prompt = render_template("refine_gate.jinja", evidence=evidence)
        model = create_model()
        resp = await model.ainvoke(
            [SystemMessage(content=prompt)],
            config={
                "run_name": "nova_oob_refine_gate",
                "tags": ["nova", "hermes", "refine-gate"],
                "metadata": {"nova_oob": True},
            },
        )
        raw = getattr(resp, "content", "")
        text = raw if isinstance(raw, str) else str(raw)
        m = re.search(r"<should_refine>\s*(true|false)\s*</should_refine>", text, re.IGNORECASE)
        if not m:
            return True, "no <should_refine> verdict in gate response"
        should = m.group(1).strip().lower() == "true"
        reason_m = re.search(r"<rationale>(.*?)</rationale>", text, re.DOTALL | re.IGNORECASE)
        reason = reason_m.group(1).strip() if reason_m else ""
        return should, reason
    except Exception:
        logger.exception("Refine gate failed — defaulting to run")
        return True, "gate error (fail-open)"


async def plan_refinements(store: BaseStore, tracker: ToolUsageTracker) -> list[dict[str, str]]:
    """Produce a refinement plan from trajectory + state evidence.

    Returns a list of item dicts (``domain``/``target``/``action``/``reason``/
    ``change``). Pure planning — nothing is applied. Best-effort: on any failure
    returns an empty plan.
    """
    try:
        from novacode_cli.config.model_create import create_model

        evidence = await _gather_evidence(store, tracker)
        skills = _list_skills(_user_skills_dir())
        prompt_state = _prompt_state()

        prompt = render_template(
            "refine_plan.jinja",
            skills=skills,
            prompt_state=prompt_state,
            evidence=evidence,
            max_items=_MAX_PLAN_ITEMS,
        )
        model = create_model()
        resp = await model.ainvoke(
            [SystemMessage(content=prompt)],
            config={
                "run_name": "nova_oob_refine_plan",
                "tags": ["nova", "hermes", "refine-plan"],
                "metadata": {"nova_oob": True},
            },
        )
        raw = getattr(resp, "content", "")
        text = raw if isinstance(raw, str) else str(raw)
        return _parse_plan(text)
    except Exception:
        logger.exception("Refine planning failed")
        return []


async def run_refine(store: BaseStore, tracker: ToolUsageTracker) -> dict[str, object]:
    """Run the full plan → apply → review → rollback loop.

    Returns a summary dict::

        {
            "planned": int,
            "applied": int,
            "accepted": int,
            "rolled_back": int,
            "items": [{domain, target, action, outcome, reason}, ...],
        }

    Never raises — every step degrades gracefully.
    """
    summary: dict[str, object] = {
        "items": [],
        "planned": 0,
        "applied": 0,
        "accepted": 0,
        "rolled_back": 0,
    }

    # Cheap pre-plan gate: skip the whole run when the trajectory is noise.
    should, gate_reason = await should_refine(store, tracker)
    if not should:
        _emit_tui_event("nova_review_complete", f"✦ /refine: skipped ({gate_reason})")
        return summary

    plan = await plan_refinements(store, tracker)
    summary["planned"] = len(plan)
    if not plan:
        _emit_tui_event("nova_review_complete", "✦ /refine: no changes proposed")
        return summary

    evidence = await _gather_evidence(store, tracker)

    for item in plan:
        domain = item["domain"]
        target = item["target"]
        action = item.get("action", "update")
        outcome = "skipped"
        before = _read_current(item)
        after = item.get("change", "")

        if domain in _REVIEW_FIRST_DOMAINS:
            # Append-only domain (memory): review BEFORE applying — there is no
            # rollback, so a rejected edit must never reach the file.
            accepted, reason = await _review(item, evidence)
            if not accepted:
                outcome = "rejected"
                _log_event(domain, action, target, "rejected", reason or item.get("reason", ""))
            else:
                applied = await _apply(item, store)
                if applied:
                    outcome = "applied"
                    summary["applied"] += 1
                    summary["accepted"] += 1
                    _log_event(
                        domain, action, target, "applied", item.get("reason", ""),
                        before=before, after=after,
                    )
                else:
                    outcome = "skipped"
                    _log_event(domain, action, target, "skipped", item.get("reason", ""))
        else:
            applied = await _apply(item, store)
            if not applied:
                outcome = "skipped"
                _log_event(domain, action, target, "skipped", item.get("reason", ""))
            else:
                summary["applied"] += 1
                accepted, reason = await _review(item, evidence)
                if accepted:
                    outcome = "applied"
                    summary["accepted"] += 1
                    _log_event(
                        domain, action, target, "applied", item.get("reason", ""),
                        before=before, after=after,
                    )
                else:
                    outcome = "rolled_back"
                    summary["rolled_back"] += 1
                    await _rollback(item)
                    _log_event(
                        domain, action, target, "rolled_back",
                        reason or item.get("reason", ""),
                        before=before, after=after,
                    )

        summary["items"].append(
            {
                "domain": domain,
                "target": target,
                "action": action,
                "outcome": outcome,
                "reason": item.get("reason", ""),
            }
        )

    _emit_tui_event(
        "nova_review_complete",
        f"✦ /refine: {summary['accepted']} applied, "
        f"{summary['rolled_back']} rolled back, {summary['planned']} proposed",
    )
    return summary
