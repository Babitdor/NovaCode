"""Skills middleware: a small listing, per-turn suggestions, and a search tool.

deepagents' :class:`SkillsMiddleware` puts every skill's name, description and
path in the system prompt on every call: ~200k chars for Nova's ~630 skills.
This subclass keeps the progressive-disclosure contract but tiers it, the way
Claude Code (1% of the window), Codex (2%) and Anthropic's Tool Search Tool do:

1. **Listing** — only the most-used skills, within a char budget. Frozen for
   the life of the middleware so the cached prompt prefix never changes.
2. **Suggestions** — each new user message is matched against the whole
   library (:mod:`novacode_cli.skills.retrieval`) and the few that clear a
   relevance bar are added as an ``Internal context`` message. Automatic,
   because agents are poor at noticing when they need a skill
   (arXiv 2604.24594); in the messages, not the system prompt, so the cache
   holds.
3. **``skills_search``** — for needs that surface mid-task.

It also re-lists when the watched skill directories change, so a skill created
mid-session (``skill_manage`` / Hermes review) is usable at once (see
``specs/2026-06-22-mid-session-skill-refresh-design.md``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents.middleware.skills import SkillsMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from novacode_cli.skills.retrieval import first_sentence, get_index, prewarm

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.middleware.skills import SkillMetadata, SkillsState, SkillsStateUpdate
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

logger = logging.getLogger("nova.skills")

#: Listing budget when the model's window is unknown.
DEFAULT_LISTING_CHARS = 4_000
#: Hard ceiling: a 1M-token window must not mean an 80k-char listing.
MAX_LISTING_CHARS = 8_000

# Starts with "Internal context" so transcript replay hides it
# (core/streaming.py::is_internal_context_text): the user did not type it.
SUGGESTION_MARKER = "Internal context: skills that may apply."

SKILLS_PROMPT = """## Skills

Skills are playbooks (`SKILL.md`) for specific tasks. {skills_load_warnings}

**Finding one.** When a request looks like it has a matching skill, you get an
`{marker}` note listing them. Mid-task, call `skills_search("<what you need>")`.
Most-used skills:

{skills_list}

**Using one.** Read its `SKILL.md` with `read_file(path, limit=1000)` and follow
it; paths inside it are absolute. Skip a suggested skill that does not fit.
Skill sources: {skills_locations}"""


def listing_budget(context_window: int) -> int:
    """Chars for the always-on listing: 1% of the window, as Claude Code does."""
    if context_window <= 0:
        return DEFAULT_LISTING_CHARS
    return min(MAX_LISTING_CHARS, int(context_window * 0.01 * 4))  # ~4 chars/token


def _entry(skill: SkillMetadata) -> str:
    return f"- **{skill['name']}** `{skill['path']}`: {first_sentence(skill['description'])}"


def _text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


class RefreshingSkillsMiddleware(SkillsMiddleware):
    """Tiered skills: budgeted listing + per-turn suggestions + ``skills_search``."""

    def __init__(
        self,
        *,
        backend: object,
        sources: Sequence[object],
        watch_dirs: Sequence[Path] = (),
        listing_chars: int = DEFAULT_LISTING_CHARS,
    ) -> None:
        """``listing_chars=0`` lists nothing (subagents: suggestions + search only)."""
        super().__init__(
            backend=backend,  # type: ignore[arg-type]
            sources=sources,  # type: ignore[arg-type]
            system_prompt=SKILLS_PROMPT.replace("{marker}", SUGGESTION_MARKER),
        )
        self._watch_dirs = [Path(d) for d in watch_dirs]
        self._last_signature: frozenset[tuple[str, float]] | None = None
        self._listing_chars = listing_chars
        self._usage: dict[str, int] | None = None  # read once, then frozen
        self._skills: list[SkillMetadata] = []
        prewarm()  # load the embedder while the user types, not on turn one
        self.tools = [
            StructuredTool.from_function(
                self._skill_search,
                name="skills_search",
                description=(
                    "Search the skill library (playbooks for specific tasks) by what you "
                    "need to do. Returns the best matches with the path of each SKILL.md."
                ),
            )
        ]

    # ── skills_search ──────────────────────────────────────────────────────

    def _skill_search(self, query: str) -> str:
        """Search the skills library.

        Args:
            query: What you are trying to do, in plain words.
        """
        hits = get_index(self._skills).search(query, k=5) if self._skills else []
        if not hits:
            return f"No skill matches {query!r}. Proceed without one."
        return "\n".join(
            f"- **{s['name']}** `{s['path']}`: {first_sentence(s['description'], 300)}"
            for s, _, _ in hits
        )

    # ── tier 1: the listing ────────────────────────────────────────────────

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """The most-used skills that fit the budget; the rest via search."""
        usage = self._usage or {}
        ranked = sorted(skills, key=lambda s: -usage.get(s["name"], 0))  # stable: source order
        lines: list[str] = []
        used = 0
        for skill in ranked:
            line = _entry(skill)
            if used + len(line) > self._listing_chars:
                break
            lines.append(line)
            used += len(line) + 1
        rest = len(skills) - len(lines)
        if rest:
            lines.append(f"- ({rest} more skills: `skills_search` finds them)")
        return "\n".join(lines)

    # ── tier 2: suggestions ────────────────────────────────────────────────

    def _suggestions(self, state: SkillsState, skills: list[SkillMetadata]) -> HumanMessage | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, HumanMessage):
            return None
        query = _text(last)
        if not query.strip() or query.lstrip().startswith("Internal context"):
            return None
        already = {
            name
            for m in messages
            if isinstance(m, HumanMessage) and _text(m).startswith(SUGGESTION_MARKER)
            for name in re.findall(r"^- \*\*([^*]+)\*\*", _text(m), re.M)
        }
        picks = get_index(skills).suggest(query[:2000], exclude=already)
        if not picks:
            return None
        body = "\n".join(_entry(s) for s in picks)
        return HumanMessage(
            content=(
                f"{SUGGESTION_MARKER} If one fits this request, read its SKILL.md "
                f"before starting; otherwise ignore this.\n{body}"
            )
        )

    async def _read_usage(self, runtime: Runtime) -> None:
        """Skill invocation counts from Hermes' tracker (best effort, once)."""
        if self._usage is not None:
            return
        self._usage = {}
        store = getattr(runtime, "store", None)
        if store is None:
            return
        try:
            for item in await store.asearch(("nova", "skill_usage"), limit=1000):
                self._usage[item.key] = int((item.value or {}).get("invocations", 0))
        except Exception:  # noqa: BLE001 — ranking is a nicety, never break a turn
            logger.debug("skill usage unavailable", exc_info=True)

    def _finish(
        self, state: SkillsState, update: SkillsStateUpdate | None
    ) -> SkillsStateUpdate | None:
        from novacode_cli.skills.skills_prefs import effective_disabled

        skills = (update or {}).get("skills_metadata", state.get("skills_metadata")) or []
        disabled = effective_disabled()
        self._skills = [s for s in skills if s["name"] not in disabled]
        try:
            note = self._suggestions(state, self._skills) if self._skills else None
        except Exception:  # noqa: BLE001 — retrieval must never break a turn
            logger.debug("skill suggestion failed", exc_info=True)
            note = None
        if note is None:
            return update
        return {**(update or {}), "messages": [note]}  # type: ignore[typeddict-unknown-key]

    # ── refresh when skill files change ────────────────────────────────────

    def _compute_signature(self) -> frozenset[tuple[str, float]]:
        """A (path, mtime) frozenset over watched ``*/SKILL.md`` files + prefs.

        The skill-curation preference files are folded in so toggling a skill
        on/off (which only rewrites ``skills_prefs.json``) forces a re-list on
        the next turn — the curated set updates without a restart.
        """
        from novacode_cli.skills.skills_prefs import prefs_signature

        sig: set[tuple[str, float]] = set()
        for directory in self._watch_dirs:
            try:
                skill_files = list(directory.glob("*/SKILL.md"))
            except OSError:
                continue
            for skill_md in skill_files:
                try:
                    sig.add((str(skill_md), skill_md.stat().st_mtime))
                except OSError:
                    continue
        return frozenset(sig | prefs_signature())

    def _skills_changed(self) -> bool:
        """True if the skill files changed since the last call (best-effort)."""
        if not self._watch_dirs:
            return False
        try:
            current = self._compute_signature()
        except Exception:  # noqa: BLE001 - best-effort; never break a turn
            return False
        changed = current != self._last_signature
        self._last_signature = current
        return changed

    def before_agent(
        self, state: SkillsState, runtime: Runtime, config: RunnableConfig
    ) -> SkillsStateUpdate | None:
        """Re-list if skills changed, load, then suggest for the new message."""
        if self._skills_changed():
            state = {k: v for k, v in state.items() if k != "skills_metadata"}  # type: ignore[assignment]
        return self._finish(state, super().before_agent(state, runtime, config))

    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime, config: RunnableConfig
    ) -> SkillsStateUpdate | None:
        """Async twin of :meth:`before_agent` (the path the agent actually runs)."""
        await self._read_usage(runtime)
        if self._skills_changed():
            state = {k: v for k, v in state.items() if k != "skills_metadata"}  # type: ignore[assignment]
        return self._finish(state, await super().abefore_agent(state, runtime, config))


class SubagentSkillsMiddleware(RefreshingSkillsMiddleware):
    """What deepagents builds for subagents: no listing, suggestions + search.

    A subagent's first message is its task description, which is exactly the
    query the suggestions need.
    """

    def __init__(self, *, backend: object, sources: Sequence[object], **_: Any) -> None:
        """Signature-compatible with deepagents' ``SkillsMiddleware(backend=, sources=)``."""
        super().__init__(backend=backend, sources=sources, listing_chars=0)
