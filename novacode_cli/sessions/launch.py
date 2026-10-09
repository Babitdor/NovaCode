"""Validation helpers for approval-gated project session tabs."""

from __future__ import annotations

import uuid
from pathlib import Path


def approved_projects() -> list[Path]:
    """Read and resolve approved folders off the UI loop, once per candidate."""
    from novacode_cli.path_approval import PathApprovalManager

    manager = PathApprovalManager()
    approved = manager.list_approved_paths()
    paths = approved.keys() if isinstance(approved, dict) else approved
    projects = {}
    for path in paths:
        try:
            target = Path(path).resolve(strict=True)
            if target.is_dir() and manager.is_path_approved(target):
                projects[str(target)] = target
        except (OSError, RuntimeError):
            # An unavailable drive/project must not prevent choosing another.
            continue
    return sorted(projects.values(), key=lambda path: str(path).casefold())


def validate_approved_folder(folder: str | Path) -> Path:
    """Re-read trust immediately before launch; callers run this off-thread."""
    from novacode_cli.path_approval import PathApprovalManager

    target = Path(folder).resolve(strict=True)
    if not target.is_dir() or not PathApprovalManager().is_path_approved(target):
        raise ValueError("The session folder is unavailable or no longer Nova-approved.")
    return target


def prepare_launch(folder: str, name: str | None, task: str, source_cwd: str) -> dict:
    """Resolve an existing project folder and freeze the details shown for approval."""
    candidate = Path(folder).expanduser()
    if not candidate.is_absolute():
        candidate = Path(source_cwd) / candidate
    target = candidate.resolve(strict=True)
    if not target.is_dir():
        raise ValueError(f"Not a directory: {target}")
    return {
        "request_id": uuid.uuid4().hex[:12],
        "folder": str(target),
        "name": (name or target.name or str(target)).strip(),
        "task": task.strip(),
    }
