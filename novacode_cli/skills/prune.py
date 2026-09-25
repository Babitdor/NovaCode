"""Prune skills — the shared core behind ``/skills prune`` and the TUI.

Both the REPL (``commands/skills_commands.py``) and the TUI
(``tui/screens.py`` ``SkillsScreen``) call into this module so their behaviour
cannot drift: locating prunable skills, archiving a selection, and listing or
restoring what was archived.

Deletion is **always** a soft delete — it delegates to
:func:`novacode_cli.skills.versioning.archive_skill`, which moves the skill
directory to ``<skills_root>/../skills-archive/``. Nothing here ever calls
``shutil.rmtree``, so every prune is recoverable via :func:`restore`.

Deliberately UI-free (no ``console``, no Textual): it returns data and the two
surfaces render it.

Note on usage counts: invocation counts live in the Hermes ``skill_usage``
store, which is only written when the learning loop is enabled (OFF by
default). They are therefore reported as *optional enrichment* — never as the
basis for selecting what to prune, which would flag every skill on a default
install.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from novacode_cli.skills import versioning

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("nova.skills.prune")

#: Sources whose skills may be archived. ``claude`` is a bundled/read-only
#: source; plugin skills are managed by their plugin and never pruned here.
PRUNABLE_SOURCES = frozenset({"user", "project"})

_DESCRIPTION_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def _read_description(skill_md: Path) -> str:
    """Frontmatter ``description`` from a SKILL.md (best-effort, "" on failure)."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _DESCRIPTION_RE.search(text)
    return match.group(1).strip().strip("\"'") if match else ""


def archive_root_for(skills_dir: Path) -> Path:
    """The archive directory that :func:`versioning.archive_skill` uses."""
    return skills_dir.parent / "skills-archive"


def _skill_dirs() -> list[tuple[Path, str]]:
    """Every directory that can hold a skill, as ``(dir, source)``.

    Order matches :func:`find_skill_dir` precedence (user → project → claude).
    Scanning these directly keeps this module free of the deepagents import that
    ``list_skills`` pulls in, so the TUI opens without a multi-second stall.
    """
    from novacode_cli.config.config import Settings

    settings = Settings.from_environment()
    dirs: list[tuple[Path, str]] = [(settings.get_global_skills_dir(), "user")]
    dirs.extend((project_dir, "project") for project_dir in settings.get_project_skills_dirs())
    dirs.append((Settings.get_global_claude_skills_dir(), "claude"))
    return dirs


def candidates(usage: dict[str, dict] | None = None) -> list[dict]:
    """Every installed skill, marked with whether it may be pruned.

    ``usage`` optionally maps skill name → ``{"invocations": int}`` (from the
    Hermes store); when omitted, counts are reported as 0. Missing usage is
    normal — the learning loop is opt-in — so callers must not read absence as
    "unused".

    Returns dicts with ``name``, ``source``, ``path``, ``description``,
    ``invocations`` and ``prunable`` (False for bundled/plugin sources).
    """
    usage = usage or {}
    out: list[dict] = []
    seen: set[str] = set()

    for directory, source in _skill_dirs():
        if not directory.exists():
            continue
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            # Dot-dirs hold version history, not skills.
            if not child.is_dir() or child.name.startswith("."):
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file() or child.name in seen:
                continue
            seen.add(child.name)
            out.append(
                {
                    "name": child.name,
                    "source": source,
                    "path": child,
                    "description": _read_description(skill_md),
                    "invocations": int(usage.get(child.name, {}).get("invocations", 0)),
                    "prunable": source in PRUNABLE_SOURCES,
                }
            )

    out.sort(key=lambda item: (item["source"], item["name"]))
    return out


def _forget_prefs(name: str) -> None:
    """Drop ``name`` from the disabled set, so no stale entry outlives it."""
    from novacode_cli.skills import skills_prefs

    for scope in ("global", "project"):
        path = skills_prefs.scope_path(scope)
        if path is None or not path.exists():
            continue
        disabled = skills_prefs.load_disabled(path)
        if name in disabled:
            disabled.discard(name)
            skills_prefs.save_disabled(path, disabled)


def archive_one(name: str) -> tuple[bool, str]:
    """Archive a single skill. Returns ``(ok, message)``.

    Mirrors the agent's ``skill_manage delete``: refuse bundled skills, move the
    directory to the archive, then drop its ``skill_lock`` entry and any stale
    curation preference.
    """
    from novacode_cli.skills.load import find_skill_dir

    resolved = find_skill_dir(name)
    if resolved is None:
        return False, f"no skill named '{name}' found"
    skill_dir, source = resolved
    if source not in PRUNABLE_SOURCES:
        return False, f"'{name}' is a bundled {source} skill and cannot be removed"

    dest = versioning.archive_skill(skill_dir)
    if dest is None:
        return False, f"could not archive '{name}'"

    # Best-effort cleanup — a failure here must not report the archive as failed.
    try:
        from novacode_cli.skills.skill_lock import SkillLock

        SkillLock.for_skills_dir(skill_dir.parent).remove(name)
    except Exception:  # noqa: BLE001
        logger.debug("Could not drop lock entry for %s", name, exc_info=True)
    try:
        _forget_prefs(name)
    except Exception:  # noqa: BLE001
        logger.debug("Could not clean prefs for %s", name, exc_info=True)

    return True, f"archived '{name}' (recoverable)"


def archive_many(names: list[str]) -> list[tuple[str, bool, str]]:
    """Archive each name in turn. Returns ``[(name, ok, message), ...]``.

    Never raises for a single failure: the caller reports per-skill outcomes so
    a partial prune is visible rather than silently swallowed.
    """
    return [(name, *archive_one(name)) for name in names]


def list_archived_skills() -> list[dict]:
    """Archived skills from both the global and project archives (newest first).

    Each entry gains a ``root`` key naming the archive it came from, which
    :func:`restore_skill` needs in order to put it back in the right place.
    """
    from novacode_cli.config.config import Settings

    settings = Settings.from_environment()
    roots: list[Path] = [archive_root_for(settings.get_global_skills_dir())]
    project_dir = settings.get_project_skills_dir()
    if project_dir is not None:
        roots.append(archive_root_for(project_dir))

    out: list[dict] = []
    for root in roots:
        out.extend({**entry, "root": root} for entry in versioning.list_archived(root))
    out.sort(key=lambda item: item["archived_at"], reverse=True)
    return out


def restore_skill(archive_dir: Path) -> tuple[bool, str]:
    """Restore an archived skill to where it came from.

    Delegates to :func:`versioning.restore_archived`, which refuses to clobber
    an existing skill of the same name.
    """
    return versioning.restore_archived(archive_dir)


__all__ = [
    "PRUNABLE_SOURCES",
    "archive_many",
    "archive_one",
    "archive_root_for",
    "candidates",
    "list_archived_skills",
    "restore_skill",
]
