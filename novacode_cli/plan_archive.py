"""Global plan archive — every approved plan, mirrored across all projects.

Plans live with the project that owns them (``<project>/.nova/plans/``), which
is right for the working copy but makes it impossible to answer "what did I
plan last week?" without remembering which checkout it was in. This module
mirrors each approved plan into ``~/.nova/plans/<project>/`` so there is one
place to browse them all.

The project copy stays authoritative: the mirror is best-effort and never
blocks or fails a plan approval. A stale or missing mirror costs nothing.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from novacode_cli.config.config import settings


def global_plans_root() -> Path:
    """The archive root, ``~/.nova/plans/``."""
    return settings.nova_dir / "plans"


def project_slug(root: Path) -> str:
    """Directory name for *root*'s plans inside the archive.

    The basename alone collides across checkouts (two ``api`` repos), so a
    short hash of the full path is appended: ``api-3f2a1b``.
    """
    import hashlib

    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:6]
    return f"{name}-{digest}"


def global_plan_dir(project_root: Path) -> Path:
    """*project_root*'s folder in the global archive, created and origin-marked.

    Where a plan Nova approved on its own is stored (see
    ``tools.plan_mode_tools.persist_approved_plan``), and where user-approved
    plans are mirrored.
    """
    dest_dir = global_plans_root() / project_slug(project_root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _write_origin_marker(dest_dir, project_root)
    return dest_dir


def mirror_plan(plan_path: Path, project_root: Path) -> Path | None:
    """Copy *plan_path* into the global archive. Returns the copy, or None.

    Best-effort by contract — callers must not let a failure here surface.
    """
    try:
        dest = global_plan_dir(project_root) / plan_path.name
        shutil.copy2(plan_path, dest)
        return dest
    except Exception:  # noqa: BLE001 - a plan approval must never fail on this
        return None


def _write_origin_marker(dest_dir: Path, project_root: Path) -> None:
    """Record which checkout a slug came from, so the listing can show it."""
    marker = dest_dir / ".origin"
    try:
        current = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if current != str(project_root):
            marker.write_text(str(project_root), encoding="utf-8")
    except OSError:
        pass


@dataclass
class ArchivedPlan:
    """One plan in the global archive."""

    path: Path
    project: str
    """Origin checkout path, or the slug when no marker was written."""
    title: str
    modified: float


def _plan_title(path: Path) -> str:
    """First markdown heading, else the filename stem."""
    try:
        with path.open(encoding="utf-8") as fh:
            # The heading is at the top or not there; don't read whole files.
            for _, line in zip(range(40), fh):
                m = re.match(r"^#+\s+(.+)$", line.strip())
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return path.stem


def list_archived_plans(limit: int | None = None) -> list[ArchivedPlan]:
    """Every archived plan, newest first."""
    root = global_plans_root()
    if not root.is_dir():
        return []

    out: list[ArchivedPlan] = []
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        marker = proj_dir / ".origin"
        try:
            origin = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        except OSError:
            origin = ""
        for plan in proj_dir.glob("*.md"):
            try:
                mtime = plan.stat().st_mtime
            except OSError:
                continue
            out.append(
                ArchivedPlan(
                    path=plan,
                    project=origin or proj_dir.name,
                    title=_plan_title(plan),
                    modified=mtime,
                )
            )

    out.sort(key=lambda p: p.modified, reverse=True)
    return out[:limit] if limit else out


def backfill_from_project(project_root: Path) -> int:
    """Mirror a project's existing plans into the archive. Returns the count.

    Plans saved before the archive existed are otherwise invisible to it.
    """
    src = project_root / ".nova" / "plans"
    if not src.is_dir():
        return 0
    n = 0
    for plan in src.glob("*.md"):
        if mirror_plan(plan, project_root) is not None:
            n += 1
    return n
