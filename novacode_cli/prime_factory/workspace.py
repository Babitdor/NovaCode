"""Committed, isolated factory workspaces and exclusive task file ownership."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any

from novacode_cli.sessions.worktree import (
    WorktreeInfo,
    _git,
    create_worktree,
    has_commits,
    repo_root,
)


class WorkspaceError(ValueError):
    """Isolation or integration cannot safely continue."""


def _run(root: Path, args: list[str]) -> str:
    code, output, error = _git(args, cwd=root)
    if code:
        raise WorkspaceError(error or "Git operation failed")
    return output


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class FactoryWorkspace:
    """Keep source files untouched and serialize integration into a dedicated branch."""

    def __init__(self, source: Path) -> None:
        """Resolve a source without changing it or allocating a worktree."""
        self.source = source.resolve()
        self.integration: WorktreeInfo | None = None
        self.baseline: str | None = None
        self.run_id: str | None = None
        self.tasks: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, str] = {}
        self._lock = RLock()

    def prepare(self, run_id: str, *, baseline: str | None = None) -> dict[str, Any]:
        """Require explicit committed-baseline selection for a dirty original checkout.

        Incorporating existing edits requires a separate reviewed commit. They are
        never copied or committed by this method.
        """
        with self._lock:
            if self.integration is not None:
                if self.run_id != run_id:
                    msg = "Workspace already belongs to another run"
                    raise WorkspaceError(msg)
                return self.snapshot()
            root = repo_root(self.source)
            if root is None or not has_commits(root):
                msg = "Implementation requires a Git repository with a commit"
                raise WorkspaceError(msg)
            if self.source != root.resolve():
                msg = "Factory isolation requires a repository-root workspace"
                raise WorkspaceError(msg)
            revision = _run(root, ["rev-parse", "HEAD"])
            dirty = _run(root, ["status", "--porcelain"])
            if baseline not in {None, "committed"}:
                msg = "Select the committed baseline or commit reviewed edits first"
                raise WorkspaceError(msg)
            if dirty and baseline is None:
                msg = (
                    f"Dirty checkout at {revision}; explicitly select baseline='committed' "
                    "to exclude existing edits, or commit reviewed edits first"
                )
                raise WorkspaceError(msg)
            slug = "factory-" + _digest([str(root), run_id])[:24]
            info = create_worktree(slug, repo=root, session_id=run_id)
            if not info.created or info.path.resolve() == self.source:
                msg = "Fresh integration isolation could not be established"
                raise WorkspaceError(msg)
            self.integration, self.baseline, self.run_id = info, revision, run_id
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Return identities for durable run records without sharing mutable task state."""
        return {
            "source": str(self.source),
            "run_id": self.run_id,
            "baseline": self.baseline,
            "path": str(self.integration.path) if self.integration else None,
            "branch": self.integration.branch if self.integration else None,
            "isolation": "worktree",
            "tasks": json.loads(json.dumps(self.tasks)),
        }

    def restore(self, record: dict[str, Any]) -> None:
        """Revalidate saved branches and retain uncertain tasks without reacquiring leases."""
        with self._lock:
            if record.get("source") != str(self.source):
                msg = "Saved workspace belongs to another source"
                raise WorkspaceError(msg)
            path = Path(record.get("path") or "").resolve()
            branch = record.get("branch")
            if path == self.source or repo_root(path) != path:
                msg = "Saved integration worktree is unavailable"
                raise WorkspaceError(msg)
            if _run(path, ["branch", "--show-current"]) != branch:
                msg = "Saved integration branch changed"
                raise WorkspaceError(msg)
            expected = "nova/factory-" + _digest([str(self.source), record.get("run_id")])[:24]
            source_git = _run(
                self.source, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
            )
            saved_git = _run(path, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
            if branch != expected or Path(source_git).resolve() != Path(saved_git).resolve():
                msg = "Saved integration worktree is not owned by this run"
                raise WorkspaceError(msg)
            code, _, _ = _git(["merge-base", "--is-ancestor", record["baseline"], "HEAD"], cwd=path)
            if code:
                msg = "Saved baseline is no longer an ancestor"
                raise WorkspaceError(msg)
            self.integration = WorktreeInfo(path=path, branch=branch, created=False, warnings=[])
            self.baseline, self.run_id = record["baseline"], record["run_id"]
            self.tasks = json.loads(json.dumps(record.get("tasks", {})))
            self._leases = {
                name: task_id
                for task_id, task in self.tasks.items()
                if task.get("status") != "integrated"
                for name in task["files"]
            }

    def _root(self) -> Path:
        if self.integration is None:
            msg = "Integration workspace has not been established"
            raise WorkspaceError(msg)
        root = self.integration.path.resolve()
        if repo_root(root) != root:
            msg = "Integration worktree is unavailable"
            raise WorkspaceError(msg)
        return root

    def _files(self, files: list[str]) -> list[str]:
        root = self._root()
        normalized = []
        for name in files:
            path = (root / name).resolve()
            if not path.is_relative_to(root) or path == root or ".git" in path.parts:
                msg = "Task ownership must stay inside the repository"
                raise WorkspaceError(msg)
            normalized.append(path.relative_to(root).as_posix())
        if not normalized:
            msg = "A development task must declare owned files"
            raise WorkspaceError(msg)
        return sorted(set(normalized))

    def task(
        self, task_id: str, *, files: list[str], depends_on: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Lease files and branch from the integrated dependency revision."""
        with self._lock:
            root = self._root()
            if task_id in self.tasks:
                msg = "Task already has a workspace"
                raise WorkspaceError(msg)
            if any(self.tasks.get(key, {}).get("status") != "integrated" for key in depends_on):
                msg = "Task dependencies are not integrated"
                raise WorkspaceError(msg)
            names = self._files(files)
            for name in names:
                for held in self._leases:
                    # Case folding also prevents collisions on Windows volumes.
                    left, right = name.casefold(), held.casefold()
                    if (
                        left == right
                        or left.startswith(right + "/")
                        or right.startswith(left + "/")
                    ):
                        msg = "Task files overlap an active task lease"
                        raise WorkspaceError(msg)
            if _run(root, ["status", "--porcelain"]):
                msg = "Integration workspace has uncommitted changes"
                raise WorkspaceError(msg)
            base = _run(root, ["rev-parse", "HEAD"])
            slug = "factory-task-" + _digest([self.run_id, task_id])[:24]
            info = create_worktree(slug, repo=root, session_id=task_id)
            if not info.created or info.path.resolve() in {root, self.source}:
                msg = "Fresh task isolation could not be established"
                raise WorkspaceError(msg)
            record = {
                "path": str(info.path),
                "branch": info.branch,
                "base": base,
                "files": names,
                "depends_on": list(depends_on),
                "status": "active",
            }
            self.tasks[task_id] = record
            self._leases.update(dict.fromkeys(names, task_id))
            return json.loads(json.dumps(record))

    def integrate(self, task_id: str, revision: str) -> str:
        """Cherry-pick only reviewed owned changes, preserving conflicts for inspection."""
        with self._lock:
            root = self._root()
            task = self.tasks[task_id]
            if task["status"] != "active":
                msg = "Task is not available for integration"
                raise WorkspaceError(msg)
            path = Path(task["path"])
            actual = _run(path, ["rev-parse", "HEAD"])
            if revision != actual or _run(path, ["status", "--porcelain"]):
                msg = "Task candidate must be its clean committed HEAD"
                raise WorkspaceError(msg)
            if any(self.tasks[key]["status"] != "integrated" for key in task["depends_on"]):
                msg = "Task dependencies are not integrated"
                raise WorkspaceError(msg)
            changed = _run(
                path, ["diff", "--no-renames", "--name-only", "-z", task["base"], revision, "--"]
            )
            changed_paths = [name for name in changed.split("\0") if name]
            if any(
                not any(name == owned or name.startswith(owned + "/") for owned in task["files"])
                for name in changed_paths
            ):
                msg = "Task changed files outside its approved ownership"
                raise WorkspaceError(msg)
            if _run(root, ["status", "--porcelain"]):
                msg = "Integration workspace is not clean"
                raise WorkspaceError(msg)
            code, _, _ = _git(["merge-base", "--is-ancestor", task["base"], revision], cwd=path)
            if code:
                msg = "Task revision does not descend from its baseline"
                raise WorkspaceError(msg)
            commits = _run(
                path, ["rev-list", "--reverse", revision, f"^{task['base']}", "--"]
            ).splitlines()
            if not commits:
                msg = "Task has no reviewable commits"
                raise WorkspaceError(msg)
            for commit in commits:
                parents = _run(path, ["rev-list", "--parents", "-n", "1", commit, "--"]).split()
                if len(parents) != 2:  # noqa: PLR2004 - One commit and one parent.
                    msg = "Task commits must form a linear history"
                    raise WorkspaceError(msg)
                touched = _run(
                    path,
                    ["diff", "--no-renames", "--name-only", "-z", parents[1], commit, "--"],
                )
                if any(
                    not any(
                        name == owned or name.startswith(owned + "/") for owned in task["files"]
                    )
                    for name in touched.split("\0")
                    if name
                ):
                    msg = "Task commit changed files outside its approved ownership"
                    raise WorkspaceError(msg)
            try:
                cherries = _run(root, ["cherry", "HEAD", revision, task["base"]]).splitlines()
                pending = [line[2:] for line in cherries if line.startswith("+ ")]
                if pending:
                    _run(
                        root, ["cherry-pick", *[commit for commit in commits if commit in pending]]
                    )
            except WorkspaceError:
                task["status"] = "conflict"
                raise
            task["status"] = "integrated"
            task["revision"] = revision
            self.release(task_id)
            return _run(root, ["rev-parse", "HEAD"])

    def release(self, task_id: str) -> None:
        """Release stopped task ownership without removing its branch or files."""
        with self._lock:
            self._leases = {name: owner for name, owner in self._leases.items() if owner != task_id}

    def candidate(
        self, *, dependencies: tuple[str, ...] = (), environment: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Bind evidence to revision, complete tracked diff, dependencies and environment."""
        with self._lock:
            root = self._root()
            revision = _run(root, ["rev-parse", "HEAD"])
            status = _run(root, ["status", "--porcelain"])
            if status:
                msg = "Candidate must be committed and clean"
                raise WorkspaceError(msg)
            inputs = {}
            for name in dependencies:
                path = (root / name).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    msg = "Candidate dependency is missing or outside workspace"
                    raise WorkspaceError(msg)
                inputs[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            record = {
                "revision": revision,
                "baseline": self.baseline,
                "branch": self.integration.branch if self.integration else None,
                "workspace": str(root),
                "diff": _run(
                    root,
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--binary",
                        self.baseline or "HEAD",
                        revision,
                    ],
                ),
                "dependencies": inputs,
                "environment": environment or {},
            }
            return {"id": _digest(record), **record}
