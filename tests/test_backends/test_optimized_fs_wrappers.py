"""Wrapper improvements on OptimizedFilesystemBackend (read/edit/glob/grep).

These are pure backend-method overrides — the deepagents filesystem tools call
``backend.read/edit/glob/grep``, so improving the backend improves the tools
without touching the package. Each test pins one wrapper.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

import novacode_cli.backends.filesystem as fs_mod
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend


@pytest.fixture
def backend(tmp_path: Path) -> OptimizedFilesystemBackend:
    return OptimizedFilesystemBackend(str(tmp_path))


# -- read: non-utf-8 recovery -------------------------------------------------


def test_read_recovers_non_utf8_file(backend: OptimizedFilesystemBackend, tmp_path: Path) -> None:
    (tmp_path / "latin.txt").write_bytes("café — naïve\nsecond".encode("cp1252"))
    result = backend.read("latin.txt")
    assert result.error is None
    assert "second" in result.file_data["content"]


def test_read_utf8_unaffected(backend: OptimizedFilesystemBackend, tmp_path: Path) -> None:
    (tmp_path / "u.txt").write_text("hello\nworld\n", encoding="utf-8")
    result = backend.read("u.txt")
    assert result.error is None
    assert result.file_data["content"] == "hello\nworld\n"


# -- edit: failure hints ------------------------------------------------------


def test_edit_hint_on_whitespace_drift(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    (tmp_path / "code.py").write_text("    def foo():\n        return 1\n", encoding="utf-8")
    res = backend.edit("code.py", "def  foo():", "def bar():")  # two spaces: no exact match
    assert res.error is not None
    assert "Hint:" in res.error
    assert "line 1" in res.error


def test_edit_hint_on_ambiguous_match(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    (tmp_path / "d.py").write_text("x=1\nx=1\n", encoding="utf-8")
    res = backend.edit("d.py", "x=1", "x=2")
    assert res.error is not None
    assert "replace_all=True" in res.error


def test_edit_success_path_unchanged(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    f = tmp_path / "ok.py"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    res = backend.edit("ok.py", "beta", "gamma")
    assert res.error is None
    assert f.read_text() == "alpha\ngamma\n"


# -- glob: prune + newest-first ----------------------------------------------


def test_glob_prunes_vendored_dirs_and_sorts_newest_first(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("x")
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (1, 1))
    os.utime(new, (10**9, 10**9))

    result = backend.glob("**/*.py")
    names = [Path(m["path"]).name for m in (result.matches or [])]
    assert "junk.py" not in names  # vendored dir pruned
    assert names.index("new.py") < names.index("old.py")  # newest first




def test_glob_never_descends_into_vendored_dirs(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    """The prune must happen DURING the walk, not after it.

    Filtering vendored hits out of the finished result gives the right answer
    but still pays to walk and ``stat()`` every one of them — on a repo with a
    real ``.venv`` that is tens of thousands of files, and glob blew deepagents'
    20s ``GLOB_TIMEOUT``. Both implementations return the same matches, so
    assert on the *cost*: count stats taken inside the pruned subtree. Timing
    would be flaky on CI; this is exact.
    """
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    for i in range(20):
        (vendored / f"junk{i}.py").write_text("x")
    (tmp_path / "real.py").write_text("x")

    touched: list[str] = []
    real_stat = Path.stat

    def spy(self, *a, **kw):
        touched.append(str(self))
        return real_stat(self, *a, **kw)

    with mock.patch.object(Path, "stat", spy):
        result = backend.glob("**/*.py")

    names = [Path(m["path"]).name for m in (result.matches or [])]
    assert names == ["real.py"]
    inside = [t for t in touched if "node_modules" in t]
    assert not inside, f"stat'd {len(inside)} paths inside a pruned dir"


def test_glob_matches_at_any_depth_like_rglob(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    """A bare pattern must still match at any depth (rglob semantics)."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "top.py").write_text("x")
    (tmp_path / "pkg" / "mid.py").write_text("x")
    (tmp_path / "pkg" / "sub" / "deep.py").write_text("x")
    (tmp_path / "pkg" / "notes.txt").write_text("x")

    names = {Path(m["path"]).name for m in (backend.glob("*.py").matches or [])}
    assert names == {"top.py", "mid.py", "deep.py"}, names


def test_glob_scopes_to_path_argument(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    """path= restricts the walk to that subtree."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "in.py").write_text("x")
    (tmp_path / "b" / "out.py").write_text("x")

    names = {Path(m["path"]).name for m in (backend.glob("*.py", path="a").matches or [])}
    assert names == {"in.py"}
# -- grep: smart-case ---------------------------------------------------------


def test_grep_lowercase_is_case_insensitive(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    (tmp_path / "s.txt").write_text("Hello WORLD\nhello there\n", encoding="utf-8")
    result = backend.grep("hello", path="s.txt")  # lower-case -> matches both
    assert len(result.matches or []) == 2


def test_grep_uppercase_stays_case_sensitive(
    backend: OptimizedFilesystemBackend, tmp_path: Path
) -> None:
    (tmp_path / "s.txt").write_text("Hello WORLD\nhello there\n", encoding="utf-8")
    result = backend.grep("Hello", path="s.txt")  # has upper-case -> exact case
    assert len(result.matches or []) == 1


# -- in-root symlink/junction: discover + read, but still block escapes --------
def test_in_root_symlinked_dir_is_discoverable_and_readable(tmp_path: Path) -> None:
    """A directory symlink/junction placed inside the root (whose target lives
    outside root) must be listed and readable — this is how skills install on
    Windows (~/.claude/skills/<name> -> ~/.agents/skills/<name>). The stock
    backend drops it because the resolved target escapes root.
    """
    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "external" / "old-coder"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: old-coder\n---\nhi\n", encoding="utf-8")

    link = root / "old-coder"
    try:
        os.symlink(external, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform/run")

    backend = OptimizedFilesystemBackend(str(root), virtual_mode=True)

    entries = backend.ls(".").entries or []
    names = {str(e.get("path", "")).strip("/").split("/")[-1] for e in entries}
    assert "old-coder" in names

    resp = backend.download_files(["/old-coder/SKILL.md"])[0]
    assert resp.error is None
    assert b"name: old-coder" in (resp.content or b"")


def test_resolve_path_still_blocks_traversal(tmp_path: Path) -> None:
    """The relaxation must not open a `..`/absolute traversal hole."""
    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)
    for bad in ("/../secrets", "/a/../../b"):
        with pytest.raises(ValueError):
            backend._resolve_path(bad)


def test_read_past_the_end_says_where_the_file_ends(tmp_path):
    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

    (tmp_path / "a.txt").write_text("\n".join(f"l{i}" for i in range(30)), encoding="utf-8")
    backend = OptimizedFilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    error = backend.read("/a.txt", offset=100, limit=20).error
    assert "The file ends at line 30" in error
    assert "offset=10" in error
    assert backend.read("/a.txt", offset=0, limit=5).error is None
