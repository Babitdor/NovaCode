"""Which lessons get recalled, and whether recalling them helped.

Memory that is only ever written to grows without limit and nobody can tell the
useful part from the rest. This keeps two counts per lesson:

* ``recalls``  — how many times it was put in front of the model;
* ``resolved`` — how many of those were at a failure that was followed by a
  non-failure. That is a weak signal (the next command may have succeeded for
  any reason), but a lesson recalled twenty times at errors that never clear is
  not earning its place, and one that is never recalled at all is dead weight.

Nothing here deletes anything: :func:`unused` and :func:`report` say what the
evidence is, and a person or a curation pass decides.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE = ".recall-stats.json"


def _key(bullet: str) -> str:
    text = " ".join(bullet.lstrip("-*• ").split()).lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]  # noqa: S324 — an id, not security


def _path(agent_dir: Path) -> Path:
    return agent_dir / "memories" / _FILE


def _read(agent_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads(_path(agent_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(agent_dir: Path, stats: dict[str, dict]) -> None:
    try:
        path = _path(agent_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stats, indent=1), encoding="utf-8")
    except OSError:
        logger.debug("could not write recall stats", exc_info=True)


def note_recalled(agent_dir: Path, bullets: list[str]) -> list[str]:
    """Count one recall for each bullet; returns their keys."""
    if not bullets:
        return []
    stats, now, keys = _read(agent_dir), time.time(), []
    for bullet in bullets:
        key = _key(bullet)
        entry = stats.setdefault(
            key, {"text": bullet.lstrip("-*• ").strip()[:160], "recalls": 0, "resolved": 0, "first": now}
        )
        entry["recalls"] += 1
        entry["last"] = now
        keys.append(key)
    _write(agent_dir, stats)
    return keys


def note_resolved(agent_dir: Path, keys: list[str]) -> None:
    """The failure these lessons were recalled at was followed by a non-failure."""
    if not keys:
        return
    stats = _read(agent_dir)
    for key in keys:
        if key in stats:
            stats[key]["resolved"] = stats[key].get("resolved", 0) + 1
    _write(agent_dir, stats)


def report(agent_dir: Path) -> list[dict]:
    """Every tracked lesson, most recalled first."""
    return sorted(_read(agent_dir).values(), key=lambda e: -e.get("recalls", 0))


def unused(agent_dir: Path, lessons: list[str], *, older_than_days: float = 30.0) -> list[str]:
    """Lessons that exist but have never been recalled, among ``lessons``.

    ``older_than_days`` cannot be checked for a lesson with no recall record (it
    has no timestamp here), so callers pass only lessons they know to be at
    least that old — typically from the topic file's own review timestamps.
    """
    del older_than_days  # documented contract; ageing is the caller's knowledge
    seen = _read(agent_dir)
    return [bullet for bullet in lessons if _key(bullet) not in seen]
