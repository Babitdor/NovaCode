"""Independent Git metadata for container workspaces, without host Git mounts."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from pathlib import Path

    from novacode_cli.teams.adapters.process import TeamWorktreeInfo


def prepare_git(info: TeamWorktreeInfo) -> Path:
    """Copy objects into the isolated workspace; never mount the parent's .git."""
    from novacode_cli.sessions.worktree import repo_root

    if repo_root(info.source) != info.source.resolve():
        message = (
            "Container Git requires a repository-root workspace; subfolder scope was preserved"
        )
        raise TeamError(message)
    metadata = info.path / ".nova-container-git"
    if metadata.exists() or metadata.resolve().parent != info.path.resolve():
        message = "Container Git metadata path is unavailable"
        raise TeamError(message)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0"
    )

    def git(*args: str) -> None:
        result = subprocess.run(  # noqa: S603 - Fixed Git argv and validated workspace paths.
            ["git", *args],  # noqa: S607 - Existing required Git executable.
            cwd=info.path,
            env=environment,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            message = "Could not prepare independent container Git metadata; workspace preserved"
            raise TeamError(message)

    git(
        "clone",
        "--bare",
        "--no-local",
        "--no-hardlinks",
        "--quiet",
        "--",
        str(info.path),
        str(metadata),
    )
    git("--git-dir", str(metadata), "config", "core.bare", "false")
    git("--git-dir", str(metadata), "config", "core.worktree", "..")
    git("--git-dir", str(metadata), "config", "--remove-section", "remote.origin")
    git("--git-dir", str(metadata), "--work-tree", str(info.path), "read-tree", "HEAD")
    (metadata / "info/exclude").write_text(".nova-container-git/\n", encoding="utf-8")
    return metadata
