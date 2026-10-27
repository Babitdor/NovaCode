"""Owner-controlled task worktrees and conservative integration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.sessions.worktree import _git, create_worktree

if TYPE_CHECKING:
    from novacode_cli.rooms.store import Record, RoomStore


def git(project: Path, *args: str) -> str:
    """Fail closed on every Git error."""
    code, out, error = _git(list(args), project)
    if code:
        raise ValueError(error or "Git command failed")
    return out


def prepare(store: RoomStore, key: str) -> Record:
    """Prepare a reviewable worktree before the owner starts execution."""
    with store.transaction():
        task = store.task(key)
        if task["status"] != "awaiting approval":
            msg = "Task has already been started"
            raise ValueError(msg)
        if not task["base"]:
            msg = "Implementation requires Git and an initial commit"
            raise ValueError(msg)
        if not task["worktree"]:
            info = create_worktree("room-" + key[:16], repo=store.project, session_id=key)
            if not info.branch or info.path.resolve() == store.project:
                msg = "An isolated Git worktree is required"
                raise ValueError(msg)
            task.update(
                worktree=str(info.path),
                branch=info.branch,
                base=git(info.path, "rev-parse", "HEAD"),
                warning="\n".join(info.warnings),
            )
            store.put_task(task)
        return task


def diff(store: RoomStore, key: str) -> str:
    """Include tracked and new-file contents without staging changes."""
    task = store.task(key)
    if not task["worktree"]:
        return "Task has no worktree yet."
    path = Path(task["worktree"])
    output = git(path, "diff", task["base"])
    files = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],  # noqa: S607
        cwd=path,
        capture_output=True,
        check=True,
    )
    for name in filter(None, os.fsdecode(files.stdout).split("\0")):
        code, patch, error = _git(["diff", "--no-index", "--", os.devnull, name], path)
        if code not in (0, 1):
            raise ValueError(error)
        output += "\n" + patch
    return output or "No changes."


def integrate(store: RoomStore, key: str) -> None:
    """Snapshot approved task work and fast-forward only a clean unchanged owner tree."""
    with store.transaction():
        task = store.task(key)
        if task["status"] != "finished":
            msg = "Only finished tasks can be integrated"
            raise ValueError(msg)
        if store.rows(
            "SELECT id FROM jobs WHERE task=? AND status IN ('queued','working')", (key,)
        ):
            msg = "Task still has pending workers"
            raise ValueError(msg)
        if git(store.project, "status", "--porcelain"):
            msg = "Project has uncommitted changes; resolve them before integration"
            raise ValueError(msg)
        if git(store.project, "rev-parse", "HEAD") != task["base"]:
            msg = "Project base changed; review and reconcile the worktree manually"
            raise ValueError(msg)
        path = Path(task["worktree"])
        if git(path, "status", "--porcelain"):
            git(path, "add", "--all")
            git(path, "commit", "-m", "Nova room task: " + task["goal"][:72])
        git(store.project, "merge", "--ff-only", task["branch"])
        task["status"] = "integrated"
        store.put_task(task)
