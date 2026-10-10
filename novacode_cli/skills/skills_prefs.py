"""Per-skill enable/disable preferences — skill curation (opt-out model).

A skill is ON unless its name appears in a ``disabled`` list. Two scopes
compose by **union**: a skill is hidden if it is disabled in *either* the
global file (``~/.nova/skills_prefs.json``) or the project file
(``{project}/.nova/skills_prefs.json``). The project file is committable so a
team can share one curated set.

File format::

    {"disabled": ["docx", "verification-cascade"]}

The core load/save helpers take an explicit ``Path`` so they're trivially
testable; the ``*_prefs_path`` / :func:`effective_disabled` resolvers read the
live :data:`settings` for the real locations.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from novacode_cli.config.config import settings

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

PREFS_FILENAME = "skills_prefs.json"

Scope = Literal["global", "project"]


# ── path resolution ──────────────────────────────────────────────────────────


def global_prefs_path() -> Path:
    """``~/.nova/skills_prefs.json`` — the global (cross-project) prefs file."""
    return settings.user_deepagents_dir / PREFS_FILENAME


def project_prefs_path() -> Path | None:
    """``{project}/.nova/skills_prefs.json``, or ``None`` if not in a project."""
    if not settings.project_root:
        return None
    return settings.project_root / ".nova" / PREFS_FILENAME


def scope_path(scope: Scope) -> Path | None:
    """Resolve a writable prefs path for ``scope`` (``None`` if unavailable)."""
    return global_prefs_path() if scope == "global" else project_prefs_path()


# ── core load / save (path-explicit, side-effect free to test) ───────────────


def load_disabled(path: Path | None) -> set[str]:
    """Return the disabled-skill names in ``path`` (empty set on any problem)."""
    if path is None or not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    disabled = data.get("disabled", [])
    if not isinstance(disabled, list):
        return set()
    return {str(name) for name in disabled}


def save_disabled(path: Path, disabled: Iterable[str]) -> None:
    """Write the disabled set to ``path`` (creates parent dirs; sorted, stable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"disabled": sorted(set(disabled))}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ── effective state (the union the agent actually sees) ──────────────────────


def effective_disabled() -> set[str]:
    """Disabled names across both scopes (the union of global and project)."""
    return load_disabled(global_prefs_path()) | load_disabled(project_prefs_path())


def is_skill_enabled(name: str) -> bool:
    """True if ``name`` is not disabled in either scope."""
    return name not in effective_disabled()


def set_skill_enabled(name: str, *, enabled: bool, scope: Scope) -> None:
    """Persist a single skill's on/off state to the given ``scope`` file.

    Raises:
        ValueError: if ``scope`` is "project" but there is no project.
    """
    path = scope_path(scope)
    if path is None:
        msg = "Cannot write project skill prefs: not inside a project."
        raise ValueError(msg)
    disabled = load_disabled(path)
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    save_disabled(path, disabled)


def prefs_signature() -> frozenset[tuple[str, str]]:
    """A ``(path, content digest)`` set over the existing prefs files.

    Feeds the skills-middleware refresh check so toggling a skill re-lists the
    available set on the next turn without a restart.
    """
    sig: set[tuple[str, str]] = set()
    for path in (global_prefs_path(), project_prefs_path()):
        if path is not None and path.exists():
            try:
                from novacode_cli.computation_cache import digest

                sig.add((str(path), digest(path.read_bytes())))
            except OSError:
                continue
    return frozenset(sig)
