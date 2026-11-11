"""Learning overview — a compact at-a-glance view of Nova's learning state.

Inspired by Prime Agent's ``harness.overview()`` (see
``docs/PRIME-AGENT-LEARNING-ANALYSIS.md``). Nova already persists memory,
skills, and prompt-evolution state, but the pieces are scattered across
``agent.md``, ``memories/INDEX.md``, ``HABITS.md``, the skill list, and the
prompt-history manifest. This module aggregates them into a single compact
``<learning_overview>`` block injected into the system prompt, so the agent can
see what it knows, what it can do, and what it is currently improving — and act
on that view.

The overview is best-effort and read-only: any missing or unreadable source is
simply omitted. It never mutates learning state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("nova.hermes.overview")

#: Hard cap on the rendered overview length (chars). Keeps context bloat bounded.
_OVERVIEW_CHAR_CAP = 900
#: How many recent refinement events to surface in the overview.
_MAX_RECENT_EVENTS = 3
#: Max skill names to list before truncating with an ellipsis.
_MAX_SKILLS = 12


def _read_text(path: Path) -> str | None:
    """Read a file's text, or None if missing/unreadable."""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.debug("overview: could not read %s", path)
    return None


def _count_topics(index_text: str | None) -> int:
    """Count topic entries in an INDEX.md (lines that are markdown links)."""
    if not index_text:
        return 0
    return sum(1 for line in index_text.splitlines() if line.strip().startswith("- ["))


def _list_skills(skills_dir: Path) -> list[str]:
    """List skill names (directories containing a SKILL.md) under a skills dir."""
    if not skills_dir.is_dir():
        return []
    names: list[str] = []
    try:
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                names.append(child.name)
    except OSError:
        return []
    return names


def _prompt_status(prompt_history_dir: Path) -> str | None:
    """Summarize prompt-evolution state from the manifest, if present.

    Returns a short string like ``"3 templates under A/B test"`` or ``None``.
    """
    manifest_path = prompt_history_dir / "manifest.json"
    manifest = _read_text(manifest_path)
    if not manifest:
        return None
    try:
        data = json.loads(manifest)
    except (json.JSONDecodeError, TypeError):
        return None
    entries = data.get("templates") or data.get("entries") or {}
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        return None
    active = sum(1 for e in entries if isinstance(e, dict) and e.get("has_active"))
    candidate = sum(1 for e in entries if isinstance(e, dict) and e.get("has_candidate"))
    parts = []
    if active:
        parts.append(f"{active} active override(s)")
    if candidate:
        parts.append(f"{candidate} candidate(s) under A/B test")
    return ", ".join(parts) if parts else None


def _recent_events(refinement_log_path: Path) -> list[str]:
    """Return the most recent refinement events as short strings."""
    text = _read_text(refinement_log_path)
    if not text:
        return []
    try:
        events = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(events, list):
        return []
    out: list[str] = []
    for ev in events[-_MAX_RECENT_EVENTS:]:
        if not isinstance(ev, dict):
            continue
        domain = ev.get("domain", "?")
        action = ev.get("action", "?")
        target = ev.get("target", "")
        out.append(f"{domain}:{action} {target}".strip())
    return out


def build_learning_overview(
    *,
    agent_dir: Path,
    skills_dir: Path | None = None,
    prompt_history_dir: Path | None = None,
    refinement_log_path: Path | None = None,
) -> str:
    """Build a compact ``<learning_overview>`` block from Nova's learning state.

    Args:
        agent_dir: The agent's ``~/.nova/{agent}/`` directory (holds memories/).
        skills_dir: Optional skills directory (defaults to ``agent_dir/skills``).
        prompt_history_dir: Optional prompt-history dir (defaults to
            ``agent_dir/prompt_history``).
        refinement_log_path: Optional path to the refinement event log (defaults
            to ``agent_dir/refinement_events.json``).

    Returns:
        A rendered overview block, or ``""`` if nothing is available.
    """
    mem_dir = agent_dir / "memories"
    index_text = _read_text(mem_dir / "INDEX.md")
    topic_count = _count_topics(index_text)

    skills_dir = skills_dir or (agent_dir / "skills")
    skills = _list_skills(skills_dir)

    prompt_history_dir = prompt_history_dir or (agent_dir / "prompt_history")
    prompt_status = _prompt_status(prompt_history_dir)

    refinement_log_path = refinement_log_path or (agent_dir / "refinement_events.json")
    events = _recent_events(refinement_log_path)

    lines: list[str] = []
    if topic_count:
        lines.append(f"Memory: {topic_count} topic(s) in /memories/memories/INDEX.md")
    if skills:
        shown = ", ".join(skills[:_MAX_SKILLS])
        if len(skills) > _MAX_SKILLS:
            shown += f", +{len(skills) - _MAX_SKILLS} more"
        lines.append(f"Skills: {shown}")
    if prompt_status:
        lines.append(f"Prompt evolution: {prompt_status}")
    if events:
        lines.append("Recent refinements: " + "; ".join(events))

    if not lines:
        return ""

    body = "\n".join(lines)
    if len(body) > _OVERVIEW_CHAR_CAP:
        body = body[:_OVERVIEW_CHAR_CAP].rstrip() + "…"

    return (
        "<learning_overview>\n"
        "A compact view of your current learning state (memory topics, skills, "
        "prompt evolution, recent refinements). Use it to decide what to read, "
        "refine, or consolidate:\n\n"
        + body
        + "\n</learning_overview>"
    )
