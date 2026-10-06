"""Prompt-template hill climbing — Loop-Engineering Enhancement 2.

The review/evolution engines already learn, but only ever write to ``MEMORY.md``
or mint skills — never the system prompts themselves. This engine closes that
loop: when reviews repeatedly flag the *same* class of misunderstanding (an LLM
emits ``<prompt_issue template="...">`` in enough consecutive reviews), it asks a
model to propose a targeted rewrite of that ``.jinja`` template, A/B tests the
candidate against the current version using the inline verifier's pass/fail as
the quality signal, and promotes or discards it.

Safety
------
- Candidates and promotions are written **only** under ``~/.nova/prompt_history/``
  (see :data:`novacode_cli.prompts.PROMPT_HISTORY_DIR`); the packaged ``.jinja``
  files are never touched, so ``/prompt rollback`` is always able to revert by
  deleting user-space override files.
- Template names from model output are validated against the packaged templates
  (no path traversal, no inventing files).
- Best-effort throughout: any failure logs and leaves the active prompt in place.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage

from novacode_cli.events import cap_event_log, nova_event_log
from novacode_cli.hermes import config
from novacode_cli.prompts import (
    PROMPT_HISTORY_DIR,
    TEMPLATES_DIR,
    current_variant,
    render_template,
    reset_ab_choices,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel
    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.prompt_evolution")

_NEW_TEMPLATE_RE = re.compile(r"<new_template>(.*?)</new_template>", re.IGNORECASE | re.DOTALL)
_PROMPT_ISSUE_RE = re.compile(
    r'<prompt_issue\s+template="([^"]+)"\s*>(.*?)</prompt_issue>',
    re.IGNORECASE | re.DOTALL,
)
_SAFE_NAME_RE = re.compile(r"^[a-z0-9_]+\.jinja$")

#: How many of the last N reviews must flag the same template to trigger a rewrite.
#: 2-of-5 (not 3): the review prompt now flags a template on a single clear case
#: rather than only a repeating pattern, so requiring 3 hits made the trigger
#: practically unreachable (0 evolutions across 1300+ reviews). A staged
#: candidate is still A/B-tested before promotion, so 2/5 isn't trigger-happy.
_PERSIST_WINDOW = 5
_PERSIST_HITS = 2
#: A/B promotion margin (candidate pass-rate must beat active by this) + the run
#: count after which an inconclusive test is abandoned.
_AB_MARGIN = 0.05
_AB_HARD_CAP = 50


def _decide_ab(records: list[dict[str, Any]]) -> str:
    """Decide an A/B test from outcome records: promote / discard / continue.

    ``records`` are ``{"variant": "candidate"|"active", "passed": bool}``.
    """
    cand = [r for r in records if r.get("variant") == "candidate"]
    act = [r for r in records if r.get("variant") == "active"]
    if len(cand) < config.PROMPT_AB_MIN_RUNS or len(act) < config.PROMPT_AB_MIN_RUNS:
        return "continue"
    cand_rate = sum(bool(r.get("passed")) for r in cand) / len(cand)
    act_rate = sum(bool(r.get("passed")) for r in act) / len(act)
    if cand_rate >= act_rate + _AB_MARGIN:
        return "promote"
    if cand_rate < act_rate:
        return "discard"
    if len(records) >= _AB_HARD_CAP:
        return "discard"
    return "continue"


def parse_prompt_issues(content: str) -> list[tuple[str, str]]:
    """Extract ``(template, description)`` pairs from a review's ``<prompt_issue>``."""
    return [(m.group(1).strip(), m.group(2).strip()) for m in _PROMPT_ISSUE_RE.finditer(content)]


def _parse_new_template(content: str) -> str:
    """Extract the body of a ``<new_template>`` block (or ``""``)."""
    match = _NEW_TEMPLATE_RE.search(content)
    return match.group(1).strip() if match else ""


class PromptEvolutionEngine:
    """Detects persistent prompt issues and evolves the offending templates.

    Args:
        store: Durable store (A/B log + version pointers). ``None`` keeps the
            engine usable for file ops but disables persistence.
        prompts_dir: Packaged templates dir (defaults to the prompts package).
        model: Injected chat model for tests; otherwise ``create_model()``.
        spawn: Fire-and-forget task launcher (defaults to a tracked internal one).
        enabled: When ``False`` all detection/evolution is a no-op.
    """

    def __init__(
        self,
        store: BaseStore | None = None,
        *,
        prompts_dir: Path | None = None,
        model: BaseChatModel | None = None,
        spawn: Callable[[Any], None] | None = None,
        enabled: bool = True,
    ) -> None:
        """Wire dependencies; see the class docstring for argument meanings."""
        self._store = store
        self._prompts_dir = prompts_dir or TEMPLATES_DIR
        self._model = model
        self._spawn = spawn
        self._enabled = enabled
        self._tasks: set[Any] = set()

    # -- Detection ----------------------------------------------------------

    async def detect_and_maybe_evolve(self, latest_content: str) -> None:
        """Trigger evolution for any template flagged in enough recent reviews."""
        if not self._enabled:
            return
        try:
            issues = parse_prompt_issues(latest_content)
            if not issues:
                return
            recent = await self._recent_review_contents(_PERSIST_WINDOW)
            for template in {t for t, _ in issues}:
                name = self._normalise_name(template)
                if name is None:
                    continue
                hits = sum(1 for c in recent if f'template="{template}"' in c)
                if hits >= _PERSIST_HITS:
                    evidence = self._gather_evidence(recent, template)
                    await self.maybe_evolve_prompt(name, evidence)
        except Exception:
            logger.exception("Prompt-issue detection failed")

    async def maybe_evolve_prompt(self, name: str, evidence: str) -> None:
        """Spawn a rewrite for ``name`` unless a candidate is already under test."""
        if not self._enabled:
            return
        safe = self._normalise_name(name)
        if safe is None:
            return
        if (self._dir(safe) / "candidate.jinja").exists():
            return  # already A/B testing a candidate for this template
        self._launch(self.run_evolution(safe, evidence))

    async def maybe_auto_evolve(self) -> None:
        """Proactively propose a candidate for a template (periodic trigger).

        Unlike :meth:`maybe_evolve_prompt` — which waits for a review to flag a
        template — this fires on a cadence and picks the first configured
        template that isn't already under A/B test, so the loop explores even
        when nothing is being complained about. Best-effort: any failure is
        logged and leaves the active prompts untouched.
        """
        if not self._enabled:
            return
        try:
            for name in config.PROMPT_EVOLVE_TEMPLATES:
                safe = self._normalise_name(name)
                if safe is None:
                    continue
                if (self._dir(safe) / "candidate.jinja").exists():
                    continue  # already testing a candidate for this template
                self._launch(self.run_evolution(safe, evidence=""))
                return  # one candidate at a time
        except Exception:
            logger.exception("Proactive prompt evolution failed")

    # -- Evolution (OOB) ----------------------------------------------------

    async def run_evolution(self, name: str, evidence: str) -> None:
        """Ask a model for a targeted rewrite and stage it as a candidate."""
        if not self._enabled:
            return
        try:
            current = self._active_source_text(name)
            prompt = render_template(
                "prompt_evolution.jinja",
                template_name=name,
                current_template=current,
                evidence=evidence,
            )
            model = self._model
            if model is None:
                from novacode_cli.config.model_create import create_model

                model = create_model()
            resp = await model.ainvoke(
                [HumanMessage(content=prompt)],
                config={
                    "run_name": "nova_prompt_evolution",
                    "tags": ["nova", "hermes", "prompt-evolution"],
                    "metadata": {"nova_oob": True},
                },
            )
            raw = getattr(resp, "content", "")
            body = _parse_new_template(raw if isinstance(raw, str) else str(raw))
            if body and body.strip() != current.strip():
                await self._write_candidate(name, body)
                _emit(f"◇ Prompt candidate proposed for {name}")
        except Exception:
            logger.exception("Prompt evolution failed for %s", name)

    # -- A/B outcome tracking ----------------------------------------------

    async def record_outcome(self, *, passed: bool) -> None:
        """Attribute a turn's pass/fail to every template under A/B test."""
        if not self._enabled or self._store is None:
            return
        try:
            for name in self._templates_with_candidate():
                variant = current_variant(name)
                if variant is None:
                    continue
                await self._append_ab(name, variant, passed=passed)
                await self.maybe_resolve_ab(name)
        except Exception:
            logger.exception("Recording prompt A/B outcome failed")

    async def maybe_resolve_ab(self, name: str) -> str | None:
        """Promote or discard the candidate once enough A/B data has accrued."""
        records = await self._read_ab(name)
        decision = _decide_ab(records)
        if decision == "promote":
            if await self.promote(name):
                _emit(f"↑ Prompt candidate promoted: {name}")
            return decision
        if decision == "discard":
            if await self.discard(name):
                _emit(f"Prompt candidate discarded (no win): {name}")
            return decision
        return None

    # -- Promotion / rollback ----------------------------------------------

    async def promote(self, name: str) -> bool:
        """Make the candidate the active override (package stays untouched)."""
        d = self._dir(name)
        candidate = d / "candidate.jinja"
        if not candidate.exists():
            return False
        (d / "active.jinja").write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
        candidate.unlink()
        manifest = self._load_manifest(name)
        manifest["active_version"] = manifest.get("candidate_version")
        manifest["candidate_version"] = None
        self._save_manifest(name, manifest)
        await self._store_pointer(name, manifest)
        await self._clear_ab_log(name)
        reset_ab_choices()
        self._log_refinement("promote", name)
        return True

    async def discard(self, name: str) -> bool:
        """Drop the candidate, leaving the active version in place."""
        candidate = self._dir(name) / "candidate.jinja"
        existed = candidate.exists()
        if existed:
            candidate.unlink()
        manifest = self._load_manifest(name)
        manifest["candidate_version"] = None
        self._save_manifest(name, manifest)
        await self._clear_ab_log(name)
        reset_ab_choices()
        if existed:
            self._log_refinement("discard", name)
        return existed

    async def rollback(self, name: str) -> str:
        """Undo the most recent change: drop a candidate, else revert the active."""
        d = self._dir(name)
        if (d / "candidate.jinja").exists():
            await self.discard(name)
            return "candidate discarded"
        active = d / "active.jinja"
        if active.exists():
            active.unlink()
            manifest = self._load_manifest(name)
            manifest["active_version"] = None
            self._save_manifest(name, manifest)
            await self._store_pointer(name, manifest)
            reset_ab_choices()
            self._log_refinement("rollback", name)
            return "reverted to packaged template"
        return "nothing to roll back"

    def status(self) -> list[dict[str, Any]]:
        """Return per-template override state for ``/prompt status``."""
        out: list[dict[str, Any]] = []
        if not PROMPT_HISTORY_DIR.exists():
            return out
        for sub in sorted(PROMPT_HISTORY_DIR.iterdir()):
            if not sub.is_dir():
                continue
            out.append(
                {
                    "template": f"{sub.name}.jinja",
                    "has_active": (sub / "active.jinja").exists(),
                    "has_candidate": (sub / "candidate.jinja").exists(),
                }
            )
        return out

    # -- Internals: files ---------------------------------------------------

    def _normalise_name(self, template: str) -> str | None:
        """Return a safe ``<name>.jinja`` that exists in the package, or ``None``."""
        name = template if template.endswith(".jinja") else f"{template}.jinja"
        if not _SAFE_NAME_RE.match(name):
            return None
        if not (self._prompts_dir / name).exists():
            return None
        return name

    def _dir(self, name: str) -> Path:
        return PROMPT_HISTORY_DIR / name.removesuffix(".jinja")

    def _log_refinement(self, action: str, name: str) -> None:
        """Append a prompt-evolution event to the unified audit trail.

        Best-effort: a failed append never blocks the prompt mutation.
        ``PROMPT_HISTORY_DIR`` lives at ``~/.nova/prompt_history``, so the shared
        ``~/.nova`` root is its parent.
        """
        try:
            from novacode_cli.hermes.refinement_log import append_refinement_event

            append_refinement_event(
                PROMPT_HISTORY_DIR.parent,
                domain="prompt",
                action=action,
                target=name.removesuffix(".jinja"),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not append prompt refinement event", exc_info=True)

    def _active_source_text(self, name: str) -> str:
        override = self._dir(name) / "active.jinja"
        if override.exists():
            return override.read_text(encoding="utf-8")
        return (self._prompts_dir / name).read_text(encoding="utf-8")

    async def _write_candidate(self, name: str, body: str) -> None:
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        manifest = self._load_manifest(name)
        vid = f"v{len(manifest['versions']) + 1}"
        text = body.strip() + "\n"
        (d / f"{vid}.jinja").write_text(text, encoding="utf-8")
        (d / "candidate.jinja").write_text(text, encoding="utf-8")
        manifest["versions"].append({"id": vid, "created_at": time.time()})
        manifest["candidate_version"] = vid
        self._save_manifest(name, manifest)
        await self._store_pointer(name, manifest)
        reset_ab_choices()

    def _load_manifest(self, name: str) -> dict[str, Any]:
        path = self._dir(name) / "manifest.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                logger.exception("Corrupt prompt manifest for %s; resetting", name)
        return {"versions": [], "active_version": None, "candidate_version": None}

    def _save_manifest(self, name: str, manifest: dict[str, Any]) -> None:
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # -- Internals: task spawn ---------------------------------------------

    def _launch(self, coro: Coroutine[Any, Any, Any]) -> None:
        if self._spawn is not None:
            self._spawn(coro)
            return
        import asyncio

        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _templates_with_candidate(self) -> list[str]:
        if not PROMPT_HISTORY_DIR.exists():
            return []
        return [
            f"{sub.name}.jinja"
            for sub in PROMPT_HISTORY_DIR.iterdir()
            if sub.is_dir() and (sub / "candidate.jinja").exists()
        ]

    # -- Internals: store ---------------------------------------------------

    async def _recent_review_contents(self, limit: int) -> list[str]:
        if self._store is None:
            return []
        try:
            items = await self._store.asearch(("nova", "reviews"))
        except Exception:
            logger.exception("Failed to read recent reviews")
            return []
        entries = [
            (getattr(i, "value", {}) or {})
            for i in (items or [])
            if isinstance(getattr(i, "value", None), dict)
        ]
        entries.sort(key=lambda v: v.get("timestamp", 0.0), reverse=True)
        return [str(e.get("content", "")) for e in entries[:limit]]

    def _gather_evidence(self, recent: list[str], template: str) -> str:
        hits = [
            desc for content in recent for t, desc in parse_prompt_issues(content) if t == template
        ]
        return "\n".join(f"- {h}" for h in hits[:_PERSIST_WINDOW])

    async def _store_pointer(self, name: str, manifest: dict[str, Any]) -> None:
        if self._store is None:
            return
        try:
            await self._store.aput(
                config.PROMPT_VERSIONS_NS,
                name,
                {
                    "active": manifest.get("active_version"),
                    "candidate": manifest.get("candidate_version"),
                },
            )
        except Exception:
            logger.exception("Failed to persist prompt version pointer for %s", name)

    async def _append_ab(self, name: str, variant: str, *, passed: bool) -> None:
        if self._store is None:
            return
        # Unique suffix: two turns can finish in the same millisecond, and a
        # collision here would silently drop an A/B sample.
        key = f"{name.removesuffix('.jinja')}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        try:
            await self._store.aput(
                config.PROMPT_AB_LOG_NS,
                key,
                {"template": name, "variant": variant, "passed": passed, "ts": time.time()},
            )
        except Exception:
            logger.exception("Failed to append A/B record for %s", name)

    async def _read_ab(self, name: str) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        try:
            items = await self._store.asearch(config.PROMPT_AB_LOG_NS)
        except Exception:
            logger.exception("Failed to read A/B log")
            return []
        return [
            v
            for i in (items or [])
            if isinstance(v := getattr(i, "value", None), dict) and v.get("template") == name
        ]

    async def _clear_ab_log(self, name: str) -> None:
        if self._store is None:
            return
        try:
            items = await self._store.asearch(config.PROMPT_AB_LOG_NS)
            for i in items or []:
                value = getattr(i, "value", None)
                if isinstance(value, dict) and value.get("template") == name:
                    await self._store.adelete(config.PROMPT_AB_LOG_NS, getattr(i, "key", ""))
        except Exception:
            logger.exception("Failed to clear A/B log for %s", name)


def _emit(message: str) -> None:
    """Surface a prompt-evolution notice through the TUI-safe event log."""
    try:
        nova_event_log.append(("nova_prompt_evolved", "✦", "magenta", message))
        cap_event_log()
    except Exception:
        logger.exception("Failed to emit prompt-evolution event")
