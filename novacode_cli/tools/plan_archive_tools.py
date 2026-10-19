"""Read saved plans from the workspace and its host-side global archive."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from langchain.tools import tool

from novacode_cli import plan_archive
from novacode_cli.config.config import settings


@tool
def recent_plans(action: Literal["list", "read"] = "list", path: str = "", limit: int = 10) -> str:
    """Find recent approved plans for the current project, including automatic approvals.

    List searches project .nova/plans and this project's ~/.nova/plans archive,
    newest first, without repeating identical mirrored plans. Read returns full
    Markdown using a project/name.md or global/name.md path from the listing.
    This tool is read-only; it does not create directories or approve plans.
    """
    workspace = settings.get_workspace_root()
    roots = {
        "project": workspace / ".nova" / "plans",
        "global": plan_archive.global_plans_root() / plan_archive.project_slug(workspace),
    }
    if action == "read":
        scope, separator, name = path.partition("/")
        if not separator or scope not in roots or not name.endswith(".md"):
            return "Error: use a project/name.md or global/name.md path from recent_plans."
        root = roots[scope].resolve()
        target = (root / name).resolve()
        if target.parent != root:
            return "Error: plan path must stay inside its plan folder."
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return f"Error reading saved plan: {exc}"
    if action != "list":
        return "Error: action must be list or read."
    entries = []
    seen: set[tuple[str, str]] = set()
    for scope, folder in roots.items():
        for candidate in folder.glob("*.md"):
            if candidate.resolve().parent != folder.resolve():
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
                modified = candidate.stat().st_mtime
            except (OSError, UnicodeError):
                continue
            identity = (candidate.name, content)
            if identity in seen:
                continue
            seen.add(identity)
            title = next(
                (
                    line.lstrip("#").strip()
                    for line in content.splitlines()[:40]
                    if line.startswith("# ")
                ),
                candidate.stem,
            )
            entries.append(
                {
                    "path": f"{scope}/{candidate.name}",
                    "title": title,
                    "modified": datetime.fromtimestamp(modified, UTC).isoformat(),
                }
            )
    entries.sort(key=lambda entry: entry["modified"], reverse=True)
    return json.dumps(
        {
            "project_folder": str(roots["project"]),
            "global_folder": str(roots["global"]),
            "plans": entries[: max(1, min(limit, 100))],
        }
    )
