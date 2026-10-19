"""Git worktree lifecycle for parallel Nova sessions.

Each spawned session gets its own worktree so two agents working at once cannot
overwrite each other's files. The worktree is also what binds a child process to
its workspace: ``config._find_project_root`` walks up looking for ``.git`` and
tests ``git_dir.exists()``, which is true for the ``.git`` **file** a linked
worktree carries — so a child launched with ``cwd=<worktree>`` resolves
``settings.project_root`` to that worktree at import time, with no extra plumbing.

Worktrees live under ``~/.nova/worktrees/<repo-name>/<slug>``, deliberately
*outside* the repository: an in-repo worktree would be a second full copy inside
the parent session's search scope, so every grep/glob would return doubled hits.

Nothing here ever forces or discards work. Removal only happens for a worktree
that is clean and has no commits of its own; anything else is kept and reported
so the user can merge or delete it themselves.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SLUG_SAFE = re.compile(r"[^a-z0-9._-]+")
_BRANCH_PREFIX = "nova/"


@dataclass
class WorktreeInfo:
    """A worktree bound to a session."""

    path: Path
    branch: str | None
    created: bool
    """False when an existing worktree was reused, or when there is no repo."""
    warnings: list[str]


def _git(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a git command. Always a list — this repo's own path contains spaces."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, never shell=True
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as e:
        return 1, "", str(e)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git_available() -> bool:
    """Whether a ``git`` executable is on PATH."""
    return shutil.which("git") is not None


def repo_root(start: Path | None = None) -> Path | None:
    """Top-level of the git repo containing *start*; ``None`` if not a repo.

    Uses ``--show-toplevel``, which resolves to the *main* worktree's root even
    when called from inside a linked worktree.
    """
    if not git_available():
        return None
    code, out, _ = _git(["rev-parse", "--show-toplevel"], cwd=start or Path.cwd())
    if code != 0 or not out:
        return None
    return Path(out)


def slugify(name: str, fallback: str) -> str:
    """Filesystem- and branch-safe slug. Falls back when *name* has nothing usable."""
    slug = _SLUG_SAFE.sub("-", (name or "").strip().lower()).strip("-._")
    # Keep it short: Windows path budget, and these nest under the repo name.
    return (slug or fallback)[:40]


def worktrees_root() -> Path:
    """``~/.nova/worktrees`` — resolved lazily so tests can redirect HOME_DIR."""
    from novacode_cli.config.config import HOME_DIR

    return Path(HOME_DIR) / "worktrees"


def _existing_paths(repo: Path) -> set[Path]:
    """Paths git currently tracks as worktrees of *repo*."""
    code, out, _ = _git(["worktree", "list", "--porcelain"], cwd=repo)
    if code != 0:
        return set()
    found: set[Path] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            found.add(Path(line[len("worktree ") :].strip()).resolve())
    return found


def nova_worktrees(repo: Path) -> list[tuple[Path, str]]:
    """``(path, branch)`` for each worktree of *repo* on a ``nova/`` branch."""
    code, out, _ = _git(["worktree", "list", "--porcelain"], cwd=repo)
    found: list[tuple[Path, str]] = []
    path = None
    for line in out.splitlines() if code == 0 else []:
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :].strip()).resolve()
        elif line.startswith(f"branch refs/heads/{_BRANCH_PREFIX}") and path is not None:
            found.append((path, line[len("branch refs/heads/") :].strip()))
    return found


def _branch_exists(repo: Path, branch: str) -> bool:
    code, _, _ = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
    return code == 0


def has_commits(repo: Path) -> bool:
    """Whether *repo* has at least one commit (i.e. HEAD resolves).

    A freshly ``git init``-ed repository has an *unborn* HEAD: the branch exists
    but points at nothing. ``git worktree add … HEAD`` then fails with
    ``fatal: invalid reference: HEAD``, because there is no commit to check out.
    """
    code, _, _ = _git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo)
    return code == 0


def prune(repo: Path) -> None:
    """Drop git's records of worktrees whose directories are gone."""
    _git(["worktree", "prune"], cwd=repo)


def create_worktree(name: str, *, repo: Path | None = None, session_id: str = "") -> WorktreeInfo:
    """Create (or reuse) a worktree for a session.

    Returns a :class:`WorktreeInfo`. When there is no git repo, returns the
    project root itself with ``branch=None`` and a warning: refusing to spawn
    would be worse than spawning without file isolation, but the user must be
    told the sessions share files.
    """
    warnings: list[str] = []
    base = repo or repo_root()
    if base is None:
        from novacode_cli.config.config import settings

        root = Path(settings.get_workspace_root())
        warnings.append(
            "Not a git repository — this session shares files with the main session."
        )
        return WorktreeInfo(path=root, branch=None, created=False, warnings=warnings)

    # A repo with no commits has an unborn HEAD, and `worktree add … HEAD` fails
    # with "fatal: invalid reference: HEAD". Share the project root instead of
    # refusing to spawn, and say exactly how to get isolation back.
    if not has_commits(base):
        warnings.append(
            "This repository has no commits yet, and a worktree needs one to branch "
            "from — so this session shares files with the main session. Make an "
            "initial commit to get an isolated worktree."
        )
        return WorktreeInfo(path=base, branch=None, created=False, warnings=warnings)

    slug = slugify(name, fallback=f"s-{session_id[:6]}" if session_id else "session")
    branch = f"{_BRANCH_PREFIX}{slug}"
    path = (worktrees_root() / base.name / slug).resolve()

    # Already a live worktree at this path -> reuse as-is.
    if path in _existing_paths(base) and path.exists():
        return WorktreeInfo(path=path, branch=branch, created=False, warnings=warnings)

    # Stale git record, or a leftover directory -> prune once, then retry.
    if path.exists() or path in _existing_paths(base):
        prune(base)

    # Uncommitted work in the main worktree does NOT travel to the new one
    # (it branches from HEAD), so say so rather than surprising the user.
    code, dirty, _ = _git(["status", "--porcelain"], cwd=base)
    if code == 0 and dirty:
        warnings.append(
            f"{len(dirty.splitlines())} uncommitted change(s) in the main worktree "
            "are not carried into this session (it branches from HEAD)."
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    # Reuse the branch if it already exists; only create it when it doesn't.
    if _branch_exists(base, branch):
        args = ["worktree", "add", str(path), branch]
    else:
        args = ["worktree", "add", "-b", branch, str(path), "HEAD"]

    code, _, err = _git(args, cwd=base)
    if code != 0:
        prune(base)
        code, _, err = _git(args, cwd=base)
    if code != 0:
        msg = f"git worktree add failed for {path}: {err}"
        raise RuntimeError(msg)

    return WorktreeInfo(path=path, branch=branch, created=True, warnings=warnings)


def worktree_status(path: Path, *, base_branch: str = "HEAD") -> tuple[bool, int]:
    """``(dirty, commits_ahead)`` for the worktree at *path*.

    ``commits_ahead`` counts commits reachable from the worktree's HEAD but not
    from *base_branch*. Pass the **main** worktree's HEAD sha — the default
    ``"HEAD"`` resolves inside *path* itself, so it compares the worktree to
    itself and always reports 0 (:func:`remove_worktree` resolves a real base).

    Both values fail safe: if git errors, they report "has work", so a failure
    can never cause :func:`remove_worktree` to delete something.
    """
    code, out, _ = _git(["status", "--porcelain"], cwd=path)
    dirty = True if code != 0 else bool(out)

    code, out, _ = _git(["rev-list", "--count", f"{base_branch}..HEAD"], cwd=path)
    if code != 0 or not out.isdigit():
        return dirty, 1  # unknown -> treat as having work
    return dirty, int(out)


def remove_worktree(path: Path, *, repo: Path, force: bool = False) -> str:
    """Remove the worktree at *path* only when it holds no work.

    Returns a human-readable outcome. Never passes ``--force`` and never deletes
    a dirty tree or one with its own commits unless *force* is explicitly set by
    a user decision upstream.

    Call this only after the child process has exited: on Windows a directory a
    live process still holds open cannot be removed.
    """
    if not path.exists():
        prune(repo)
        return "already gone"

    # Compare against the MAIN worktree's HEAD. Using the default "HEAD" would
    # resolve inside `path` and compare the worktree to itself (always 0), which
    # silently disabled the has-commits guard and removed committed work.
    code, base_sha, _ = _git(["rev-parse", "HEAD"], cwd=repo)
    base = base_sha if code == 0 and base_sha else "HEAD"
    dirty, ahead = worktree_status(path, base_branch=base)
    if (dirty or ahead) and not force:
        bits = []
        if dirty:
            bits.append("uncommitted changes")
        if ahead:
            bits.append(f"{ahead} commit(s)")
        return f"kept ({' and '.join(bits)}) at {path}"

    code, _, err = _git(["worktree", "remove", str(path)], cwd=repo)
    if code != 0:
        logger.debug("worktree remove failed for %s: %s", path, err)
        return f"kept (could not remove: {err}) at {path}"
    prune(repo)
    return f"removed {path}"
