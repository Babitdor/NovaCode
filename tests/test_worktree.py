"""Git worktree lifecycle for parallel sessions.

The load-bearing test here is ``test_worktree_is_its_own_project_root``: the whole
parallel-session design rests on a child process launched with ``cwd=<worktree>``
resolving ``settings.project_root`` to that worktree. That works only because
``config._find_project_root`` tests ``git_dir.exists()``, which is true for the
``.git`` *file* a linked worktree carries. If that ever stops holding, children
would silently operate on the main repo and clobber each other.

The other half is the safety contract: removal must never destroy work.

Runnable directly (``python tests/test_worktree.py``) or via pytest.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from novacode_cli.sessions import worktree as wt

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A real one-commit git repo (worktrees need at least one commit for HEAD)."""
    root = tmp_path / "repo"
    root.mkdir()
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.email", "t@example.com"], root)
    _run(["git", "config", "user.name", "T"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", "init"], root)
    return root


@pytest.fixture(autouse=True)
def _isolate_worktrees_root(monkeypatch, tmp_path):
    """Keep worktrees out of the developer's real repo and ~/.nova/worktrees.

    ``create_worktree(repo=None)`` discovers the repo from cwd — which during a
    test run is the actual Nova checkout. A test that meant "no repo" once
    created a live worktree and a ``nova/x`` branch in it. Redirect HOME_DIR and
    make cwd-discovery fail loudly so that can't recur; tests that want a repo
    pass one explicitly.
    """
    from novacode_cli.config import config as cfg

    monkeypatch.setattr(cfg, "HOME_DIR", tmp_path / "nova-home")

    real_repo_root = wt.repo_root

    def _guarded(start=None):
        if start is None:
            msg = "test called repo_root() with no start dir — pass repo= explicitly"
            raise AssertionError(msg)
        return real_repo_root(start)

    monkeypatch.setattr(wt, "repo_root", _guarded)
    yield


# ── the keystone ─────────────────────────────────────────────────────────────


def test_worktree_is_its_own_project_root(repo):
    """A child with cwd=<worktree> must resolve project_root to that worktree.

    This is what lets a spawned session inherit the correct workspace with zero
    plumbing, since `settings` freezes project_root from cwd at import time.
    """
    from novacode_cli.config.config import _find_project_root

    info = wt.create_worktree("feature-a", repo=repo)

    # A linked worktree has a .git FILE, not a directory — the thing the
    # exists() check must keep tolerating.
    dot_git = info.path / ".git"
    assert dot_git.is_file(), "linked worktree should carry a .git file"

    assert _find_project_root(info.path) == info.path.resolve()
    assert _find_project_root(info.path) != repo.resolve()


# ── creation ─────────────────────────────────────────────────────────────────


def test_create_makes_worktree_and_branch(repo):
    info = wt.create_worktree("feature-a", repo=repo)
    assert info.created is True
    assert info.path.exists()
    assert info.branch == "nova/feature-a"
    assert (info.path / "README.md").read_text(encoding="utf-8") == "hello\n"

    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert str(info.path) in out.replace("/", "\\").replace("\\", "/") or info.path.name in out


def test_worktree_lives_outside_the_repo(repo):
    # An in-repo worktree would double every grep/glob hit in the parent session.
    info = wt.create_worktree("feature-a", repo=repo)
    assert repo.resolve() not in info.path.resolve().parents


def test_create_is_idempotent(repo):
    first = wt.create_worktree("feature-a", repo=repo)
    second = wt.create_worktree("feature-a", repo=repo)
    assert second.path == first.path
    assert second.created is False  # reused, not recreated


def test_reuses_existing_branch_when_worktree_gone(repo):
    info = wt.create_worktree("feature-a", repo=repo)
    branch = info.branch
    subprocess.run(["git", "worktree", "remove", str(info.path)], cwd=str(repo), check=True)

    again = wt.create_worktree("feature-a", repo=repo)
    assert again.branch == branch
    assert again.path.exists()


def test_dirty_main_worktree_warns_but_still_creates(repo):
    (repo / "scratch.txt").write_text("wip", encoding="utf-8")
    info = wt.create_worktree("feature-b", repo=repo)
    assert info.path.exists()
    assert any("uncommitted" in w for w in info.warnings)
    # The uncommitted file must NOT appear in the new worktree.
    assert not (info.path / "scratch.txt").exists()


def test_repo_without_commits_shares_root_instead_of_failing(tmp_path):
    """A freshly `git init`-ed repo has an unborn HEAD.

    `git worktree add -b <branch> <path> HEAD` then fails with
    "fatal: invalid reference: HEAD" — which surfaced to the user as
    "could not create worktree" and no session at all. Share the project root
    with a clear explanation rather than refusing to spawn.
    """
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _run(["git", "init", "-b", "main"], fresh)

    assert wt.has_commits(fresh) is False

    info = wt.create_worktree("answer", repo=fresh)
    assert info.path == fresh
    assert info.branch is None
    assert info.created is False
    assert any("no commits yet" in w for w in info.warnings)
    assert any("initial commit" in w for w in info.warnings)


def test_has_commits_true_after_a_commit(repo):
    assert wt.has_commits(repo) is True


def test_worktree_creation_works_once_committed(tmp_path):
    # The same repo becomes isolatable as soon as it has one commit.
    fresh = tmp_path / "fresh2"
    fresh.mkdir()
    _run(["git", "init", "-b", "main"], fresh)
    _run(["git", "config", "user.email", "t@example.com"], fresh)
    _run(["git", "config", "user.name", "T"], fresh)
    assert wt.create_worktree("x", repo=fresh).branch is None  # unborn HEAD

    (fresh / "f.txt").write_text("hi", encoding="utf-8")
    _run(["git", "add", "-A"], fresh)
    _run(["git", "commit", "-m", "init"], fresh)

    info = wt.create_worktree("x", repo=fresh)
    assert info.branch == "nova/x"
    assert info.created is True
    assert info.path.exists()


def test_not_a_git_repo_warns_and_shares_root(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()

    from novacode_cli.config import config as cfg

    # Force the "no repo anywhere" answer. Passing repo=None is NOT how to say
    # that — it means "discover from cwd", which would find the real Nova repo
    # and create a worktree in it.
    monkeypatch.setattr(wt, "repo_root", lambda start=None: None)
    monkeypatch.setattr(cfg.settings, "get_workspace_root", lambda: plain, raising=False)

    info = wt.create_worktree("x")
    assert info.branch is None
    assert info.created is False
    assert info.path == plain
    assert any("Not a git repository" in w for w in info.warnings)


# ── slugs ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Fix Parser", "fix-parser"),
        ("feature/AUTH", "feature-auth"),
        ("  spaced  ", "spaced"),
        ("weird!!!chars###", "weird-chars"),
    ],
)
def test_slugify(raw, expected):
    assert wt.slugify(raw, fallback="fb") == expected


def test_slugify_falls_back_when_nothing_usable():
    assert wt.slugify("!!!", fallback="s-abc123") == "s-abc123"
    assert wt.slugify("", fallback="s-abc123") == "s-abc123"


def test_slug_is_length_capped():
    assert len(wt.slugify("x" * 200, fallback="fb")) <= 40


# ── removal never destroys work ──────────────────────────────────────────────


def test_removes_clean_unchanged_worktree(repo):
    info = wt.create_worktree("tidy", repo=repo)
    outcome = wt.remove_worktree(info.path, repo=repo)
    assert "removed" in outcome
    assert not info.path.exists()


def test_keeps_worktree_with_uncommitted_changes(repo):
    info = wt.create_worktree("dirty", repo=repo)
    (info.path / "new.txt").write_text("work in progress", encoding="utf-8")

    outcome = wt.remove_worktree(info.path, repo=repo)
    assert "kept" in outcome
    assert info.path.exists()
    assert (info.path / "new.txt").read_text(encoding="utf-8") == "work in progress"


def test_keeps_worktree_with_commits(repo):
    info = wt.create_worktree("committed", repo=repo)
    (info.path / "feature.py").write_text("print(1)\n", encoding="utf-8")
    _run(["git", "add", "-A"], info.path)
    _run(["git", "commit", "-m", "feature work"], info.path)

    dirty, ahead = wt.worktree_status(info.path, base_branch="main")
    assert dirty is False
    assert ahead == 1

    outcome = wt.remove_worktree(info.path, repo=repo)
    assert "kept" in outcome
    assert info.path.exists()


def test_remove_missing_path_is_not_an_error(repo):
    assert "already gone" in wt.remove_worktree(repo / "nope", repo=repo)


def test_status_reports_dirty(repo):
    info = wt.create_worktree("s", repo=repo)
    assert wt.worktree_status(info.path, base_branch="main") == (False, 0)
    (info.path / "f.txt").write_text("x", encoding="utf-8")
    dirty, _ = wt.worktree_status(info.path, base_branch="main")
    assert dirty is True


def test_status_fails_safe_outside_a_repo(tmp_path):
    # Unknown state must read as "has work" so removal can never delete blindly.
    dirty, ahead = wt.worktree_status(tmp_path)
    assert dirty is True
    assert ahead >= 1


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))


# ── /worktree ────────────────────────────────────────────────────────────────


def test_nova_worktrees_lists_only_nova_branches(repo):
    info = wt.create_worktree("feature", repo=repo)
    _run(["git", "worktree", "add", "-b", "other", str(repo.parent / "other"), "HEAD"], repo)
    assert wt.nova_worktrees(repo) == [(info.path, "nova/feature")]


def _command(repo, monkeypatch, text, panes=()):
    import asyncio
    from types import SimpleNamespace

    from novacode_cli.tui.app import NovaApp

    monkeypatch.chdir(repo)
    spawned, logged = [], []

    async def spawn(name, task):
        spawned.append((name, task))

    app = SimpleNamespace(
        _panes=list(panes), _log=lambda t: logged.append(t.plain), spawn_session=spawn
    )
    asyncio.run(NovaApp._run_worktree_command(app, text))
    return spawned, "\n".join(logged)


def test_worktree_command_opens_a_session_in_a_new_worktree(repo, monkeypatch):
    spawned, _ = _command(repo, monkeypatch, "/worktree login page: add OAuth")
    assert spawned == [("login page", "add OAuth")]


def test_worktree_command_lists_and_removes(repo, monkeypatch):
    from types import SimpleNamespace

    info = wt.create_worktree("feature", repo=repo)
    _, shown = _command(repo, monkeypatch, "/worktree")
    assert "nova/feature" in shown

    tab = SimpleNamespace(worktree=info.path, status="idle")
    _, shown = _command(repo, monkeypatch, "/worktree remove feature", panes=[tab])
    assert "Close its session tab" in shown and info.path.exists()

    _, shown = _command(repo, monkeypatch, "/worktree remove feature")
    assert "removed" in shown and not info.path.exists()


def test_worktree_command_refuses_without_a_commit(tmp_path, monkeypatch):
    _run(["git", "init", "-b", "main"], tmp_path)
    spawned, shown = _command(tmp_path, monkeypatch, "/worktree feature")
    assert spawned == [] and "at least one commit" in shown
