"""Skill Curator — background hygiene for the skills library.

Runs periodically (piggybacked on the review cycle) and does three things:

1. **Archive unused** — skills that have never been invoked and are older than a
   threshold are soft-moved to the archive (recoverable via the same mechanism
   as a soft-delete). Keeps the library lean without losing anything.
2. **Flag overlaps** — near-duplicate skills (by name + description similarity)
   are reported via an event so you can decide whether to merge. The curator
   never merges automatically (deliberate: a wrong merge is destructive).
3. **Track continuously** — every pass appends to a ``curation_log`` so the
   improvements are auditable over time.

Scope safety: the curator only ever scans the *user* skills directory passed to
it, so bundled (``~/.claude``) skills are never touched.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.curator")

_DEFAULT_UNUSED_AGE_DAYS = 14
_DEFAULT_OVERLAP_THRESHOLD = 0.6
_TOP_USED_N = 5  # how many most-used skills to surface per curation pass
_STOPWORDS = frozenset(
    {"the", "a", "an", "to", "for", "of", "and", "or", "with", "when", "use",
     "this", "skill", "using", "your", "you", "in", "on", "it", "is", "that"}
)


def _emit(event_type: str, message: str) -> None:
    try:
        from novacode_cli.events import nova_event_log

        icons = {
            "nova_skill_archived": ("▤", "yellow"),
            "nova_skill_overlap": ("⇄", "cyan"),
            "nova_skill_topused": ("★", "cyan"),
        }
        icon, color = icons.get(event_type, ("•", "cyan"))
        nova_event_log.append((event_type, icon, color, message))
    except Exception:  # noqa: BLE001
        logger.debug("curator event (not surfaced): %s — %s", event_type, message)


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _read_description(skill_md: Path) -> str:
    """Pull the frontmatter ``description`` from a SKILL.md (best-effort)."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else ""


def _scan_skills(skills_dir: Path) -> dict[str, tuple[Path, str]]:
    """Return ``{name: (skill_dir, description)}`` for user skills on disk.

    Skips dot-directories (``.history``, ``.archive``) so only real skills are
    considered.
    """
    out: dict[str, tuple[Path, str]] = {}
    if not skills_dir.exists():
        return out
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if skill_md.exists():
            out[child.name] = (child, _read_description(skill_md))
    return out


async def _store_map(store: BaseStore, namespace: tuple[str, str]) -> dict[str, dict]:
    try:
        results = await store.asearch(namespace)
        return {
            item.key: dict(item.value)
            for item in results
            if hasattr(item, "key") and isinstance(getattr(item, "value", None), dict)
        }
    except Exception:  # noqa: BLE001
        return {}


async def run_curation(
    store: BaseStore,
    skills_dir: Path,
    *,
    unused_age_days: int = _DEFAULT_UNUSED_AGE_DAYS,
    overlap_threshold: float = _DEFAULT_OVERLAP_THRESHOLD,
    now: float | None = None,
) -> dict[str, list]:
    """Run one curation pass. Returns ``{"archived": [...], "overlaps": [...]}``.

    Only archives skills the agent created (tracked in ``created_skills``) so a
    user's hand-written skill is never auto-archived for being unused.
    """
    from novacode_cli.skills import versioning

    now = now if now is not None else time.time()
    skills = _scan_skills(skills_dir)
    usage = await _store_map(store, ("nova", "skill_usage"))
    created = await _store_map(store, ("nova", "created_skills"))

    archived: list[str] = []
    for name, (skill_dir, _desc) in list(skills.items()):
        # Only auto-archive agent/review-created skills (safe to reclaim).
        if name not in created:
            continue
        invocations = int(usage.get(name, {}).get("invocations", 0))
        if invocations > 0:
            continue
        created_ts = float(created.get(name, {}).get("timestamp", 0.0))
        if created_ts <= 0:
            try:
                created_ts = (skill_dir / "SKILL.md").stat().st_mtime
            except OSError:
                created_ts = now
        age_days = (now - created_ts) / 86400.0
        if age_days < unused_age_days:
            continue
        if versioning.archive_skill(skill_dir) is not None:
            archived.append(name)
            skills.pop(name, None)
            _emit(
                "nova_skill_archived",
                f"▤ Curator archived unused skill '{name}' "
                f"(0 invocations, {age_days:.0f}d old) — recoverable",
            )

    # Overlap detection (flag only — never auto-merge).
    overlaps: list[dict[str, Any]] = []
    items = [(n, _tokens(f"{n} {d}")) for n, (_p, d) in skills.items()]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            score = _jaccard(items[i][1], items[j][1])
            if score >= overlap_threshold:
                overlaps.append({"a": items[i][0], "b": items[j][0], "score": round(score, 2)})
                _emit(
                    "nova_skill_overlap",
                    f"⇄ Curator: '{items[i][0]}' and '{items[j][0]}' overlap "
                    f"({score:.0%}) — consider merging",
                )

    # Most-used skills (visibility into what's actually earning its keep).
    top_used = [
        {"name": name, "invocations": int(u.get("invocations", 0))}
        for name, u in usage.items()
        if int(u.get("invocations", 0)) > 0
    ]
    top_used.sort(key=lambda x: x["invocations"], reverse=True)
    top_used = top_used[:_TOP_USED_N]
    if top_used:
        summary = ", ".join(f"{t['name']} ({t['invocations']})" for t in top_used)
        _emit("nova_skill_topused", f"★ Most-used skills: {summary}")

    # Continuous tracking: append to the curation log.
    if archived or overlaps or top_used:
        try:
            await store.aput(
                ("nova", "curation_log"),
                f"curation_{int(now)}",
                {
                    "ts": now,
                    "archived": archived,
                    "overlaps": overlaps,
                    "top_used": top_used,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not write curation_log", exc_info=True)

    return {"archived": archived, "overlaps": overlaps, "top_used": top_used}


__all__ = ["run_curation"]
