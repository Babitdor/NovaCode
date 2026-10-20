"""Real Git isolation and committed-work preservation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.sessions import worktree
from novacode_cli.teams.adapters.process import _preserve_or_remove, strict_worktree

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def initialize(path: Path) -> None:
    path.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "offline@example.invalid"],
        ["config", "user.name", "Offline"],
    ):
        assert worktree._git(args, cwd=path)[0] == 0
    (path / "source.txt").write_text("baseline")
    assert worktree._git(["add", "."], cwd=path)[0] == 0
    assert worktree._git(["commit", "-m", "baseline"], cwd=path)[0] == 0


async def test_committed_worktree_is_preserved_against_original_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "project"
    initialize(source)
    monkeypatch.setattr(worktree, "worktrees_root", lambda: tmp_path / "workers")
    info = strict_worktree(source, "committed")
    assert info.path != source
    assert info.source == source
    (info.path / "source.txt").write_text("worker changes")
    assert worktree._git(["add", "."], cwd=info.path)[0] == 0
    assert worktree._git(["commit", "-m", "worker change"], cwd=info.path)[0] == 0
    note = await _preserve_or_remove(info.path, info.source)
    assert "kept" in note
    assert info.path.exists()
    assert (source / "source.txt").read_text() == "baseline"


async def test_dirty_worktree_preserved_and_clean_tree_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "project"
    initialize(source)
    monkeypatch.setattr(worktree, "worktrees_root", lambda: tmp_path / "workers")
    dirty = strict_worktree(source, "dirty")
    (dirty.path / "untracked.txt").write_text("evidence")
    assert "kept" in await _preserve_or_remove(dirty.path, dirty.source)
    clean = strict_worktree(source, "clean")
    assert "removed" in await _preserve_or_remove(clean.path, clean.source)
    assert not clean.path.exists()


def test_container_git_is_independent_and_has_no_host_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.teams.container_git import prepare_git

    source = tmp_path / "project"
    initialize(source)
    monkeypatch.setattr(worktree, "worktrees_root", lambda: tmp_path / "workers")
    info = strict_worktree(source, "container")
    metadata = prepare_git(info)
    assert (metadata / "objects").is_dir()
    assert not (metadata / "objects/info/alternates").exists()
    config = (metadata / "config").read_text()
    assert "[remote" not in config
    code, output, _ = worktree._git(
        ["--git-dir", str(metadata), "--work-tree", str(info.path), "status", "--porcelain"],
        cwd=info.path,
    )
    assert code == 0
    assert output == ""
