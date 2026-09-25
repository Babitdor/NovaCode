"""Skill versioning + rollback safety.

Every mutation of a skill (create / patch / edit / refine) snapshots the prior
``SKILL.md`` first, so a buggy or ineffective change can be reverted instantly.
Deletes are *soft* — the skill directory is moved to a sibling archive rather
than removed — so it, too, is recoverable.

Layout::

    <skills_root>/<name>/SKILL.md
    <skills_root>/<name>/.history/<version>.md      # snapshots
    <skills_root>/<name>/.history/manifest.json     # version log
    <skills_root>/../skills-archive/<name>-<ts>/    # soft-deleted skills

The ``.history`` directory lives *inside* the skill dir (it has no ``SKILL.md``
so the skill lister ignores it, and the supporting-file loader skips dot-dirs).
The archive lives *outside* the skills root so archived skills are never listed.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nova.skills.versioning")

_HISTORY_DIR = ".history"
_MANIFEST = "manifest.json"
_ARCHIVE_DIRNAME = "skills-archive"
#: Records where an archived skill came from, so it can be restored.
_ORIGIN_FILE = "origin.json"

#: ``<name>-<timestamp>`` where timestamp is ``_now_version()``-shaped. Used to
#: recover the name from archives written before ``origin.json`` existed. The
#: trailing ``_NNNN`` tie-breaker is optional: it was added when two archives
#: sharing a microsecond timestamp silently collided, so older archives lack it.
_ARCHIVE_NAME_RE = re.compile(r"^(?P<name>.+?)-(?P<ts>\d{8}T\d{6}_\d{6}(?:_\d{4})?)$")


_version_seq = itertools.count()


def _now_version() -> str:
    """A sortable, collision-resistant version id.

    The timestamp alone was NOT collision-resistant: the system clock can be
    coarse enough (notably on Windows) that two snapshots taken back-to-back
    produce the same string, so the second silently overwrote the first and
    restoring the earlier version handed back the later content. A per-process
    counter breaks ties while keeping ids lexicographically sortable — equal
    timestamps fall back to creation order.
    """
    return f"{datetime.now().strftime('%Y%m%dT%H%M%S_%f')}_{next(_version_seq):04d}"


def _manifest_path(skill_dir: Path) -> Path:
    return skill_dir / _HISTORY_DIR / _MANIFEST


def _read_manifest(skill_dir: Path) -> dict:
    path = _manifest_path(skill_dir)
    if not path.exists():
        return {"versions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("versions"), list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"versions": []}


def _write_manifest(skill_dir: Path, data: dict) -> None:
    path = _manifest_path(skill_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def snapshot(skill_dir: Path, reason: str, *, source: str = "agent") -> str | None:
    """Snapshot the current ``SKILL.md`` before it is overwritten.

    Returns the new version id, or ``None`` if there was nothing to snapshot
    (no SKILL.md yet) or the write failed. Best-effort — never raises.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        version = _now_version()
        hist = skill_dir / _HISTORY_DIR
        hist.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_md, hist / f"{version}.md")

        data = _read_manifest(skill_dir)
        data["versions"].append(
            {
                "id": version,
                "file": f"{version}.md",
                "reason": reason,
                "source": source,
                "ts": datetime.now().timestamp(),
            }
        )
        _write_manifest(skill_dir, data)
        return version
    except OSError:
        logger.debug("Could not snapshot skill at %s", skill_dir, exc_info=True)
        return None


def list_versions(skill_dir: Path) -> list[dict]:
    """Return the version history (newest last) for a skill."""
    return _read_manifest(skill_dir).get("versions", [])


def restore(skill_dir: Path, version: str | None = None) -> tuple[bool, str]:
    """Restore a skill's ``SKILL.md`` from a snapshot.

    Snapshots the *current* state first (reason ``pre-rollback``) so the
    rollback is itself reversible, then restores either the named ``version``
    or the most recent snapshot.

    Returns ``(ok, message)``.
    """
    versions = list_versions(skill_dir)
    if not versions:
        return False, "no version history to roll back to"

    if version is None:
        target = versions[-1]
    else:
        target = next((v for v in versions if v["id"] == version), None)
        if target is None:
            return False, f"version '{version}' not found"

    snap_file = skill_dir / _HISTORY_DIR / target["file"]
    if not snap_file.exists():
        return False, f"snapshot file for '{target['id']}' is missing"

    try:
        snapshot(skill_dir, reason="pre-rollback", source="rollback")
        shutil.copy2(snap_file, skill_dir / "SKILL.md")
        return True, f"restored version {target['id']} ({target.get('reason', '')})"
    except OSError as exc:
        return False, f"restore failed: {exc}"


def archive_skill(skill_dir: Path) -> Path | None:
    """Soft-delete: move the skill dir to a sibling archive. Recoverable.

    The archive lives at ``<skills_root>/../skills-archive/`` so archived
    skills are outside any skills source and never listed. Returns the archive
    path, or ``None`` on failure.

    An ``origin.json`` is written into the archive recording the original
    location, so :func:`restore_archived` can put it back.
    """
    skills_root = skill_dir.parent
    archive_root = skills_root.parent / _ARCHIVE_DIRNAME
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = archive_root / f"{skill_dir.name}-{_now_version()}"
        shutil.move(str(skill_dir), str(dest))
    except OSError:
        logger.debug("Could not archive skill %s", skill_dir, exc_info=True)
        return None

    # Best-effort provenance: a failure here must not undo a successful move.
    try:
        (dest / _ORIGIN_FILE).write_text(
            json.dumps(
                {
                    "name": skill_dir.name,
                    "origin": str(skill_dir.resolve()),
                    "archived_at": datetime.now().timestamp(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug("Could not write origin.json for %s", dest, exc_info=True)
    return dest


def _archive_name(archive_dir: Path) -> str:
    """The skill name an archive holds: ``origin.json``, else the dir name.

    Archives written before ``origin.json`` existed are named
    ``<name>-<timestamp>``; strip that suffix. Falls back to the raw dir name
    when the pattern does not match.
    """
    origin = archive_dir / _ORIGIN_FILE
    try:
        data = json.loads(origin.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
    except (OSError, json.JSONDecodeError):
        pass
    match = _ARCHIVE_NAME_RE.match(archive_dir.name)
    return match.group("name") if match else archive_dir.name


def list_archived(archive_root: Path) -> list[dict]:
    """Return archived skills (newest first) as ``{name, path, archived_at}``.

    Never raises: a missing directory or an unreadable entry yields ``[]`` /
    is skipped, so callers can treat the archive as optional.
    """
    if not archive_root.exists():
        return []
    found: list[dict] = []
    try:
        entries = [p for p in archive_root.iterdir() if p.is_dir()]
    except OSError:
        return []
    for entry in entries:
        try:
            archived_at = entry.stat().st_mtime
        except OSError:
            archived_at = 0.0
        found.append({"name": _archive_name(entry), "path": entry, "archived_at": archived_at})
    found.sort(key=lambda item: item["archived_at"], reverse=True)
    return found


def restore_archived(archive_dir: Path, dest_root: Path | None = None) -> tuple[bool, str]:
    """Move an archived skill back into a skills directory.

    ``dest_root`` defaults to the origin recorded in ``origin.json``; when that
    is unavailable the archive's parent sibling (``<archive_root>/../skills``)
    is used, which is where :func:`archive_skill` moved it from.

    Refuses to clobber an existing skill of the same name — restoring is
    explicit and must never silently overwrite live work.

    Returns ``(ok, message)``.
    """
    if not archive_dir.exists():
        return False, f"archive '{archive_dir.name}' no longer exists"
    name = _archive_name(archive_dir)

    if dest_root is None:
        dest_root = _origin_root(archive_dir)
    if dest_root is None:
        return False, f"cannot determine where '{name}' came from"

    target = dest_root / name
    if target.exists():
        return False, f"'{name}' already exists at {target} — remove it first"

    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archive_dir), str(target))
    except OSError as exc:
        return False, f"restore failed: {exc}"

    # Drop the provenance marker: it describes the archive, not the live skill.
    with contextlib.suppress(OSError):
        (target / _ORIGIN_FILE).unlink()
    return True, f"restored '{name}' to {target}"


def _origin_root(archive_dir: Path) -> Path | None:
    """The skills directory an archive should be restored into."""
    origin = archive_dir / _ORIGIN_FILE
    try:
        data = json.loads(origin.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("origin"):
            return Path(str(data["origin"])).parent
    except (OSError, json.JSONDecodeError):
        pass
    # Legacy archives: <skills_root>/../skills-archive/<name>-<ts>
    return archive_dir.parent.parent / "skills"


__all__ = [
    "archive_skill",
    "list_archived",
    "list_versions",
    "restore",
    "restore_archived",
    "snapshot",
]
