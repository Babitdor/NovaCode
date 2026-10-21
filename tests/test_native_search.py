"""Native discovery, failure fallback, and real-executable search contracts."""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from novacode_cli.backends import OptimizedFilesystemBackend
from novacode_cli.backends import native_search as N


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("hello café\nHello again\nother\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("hello elsewhere\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.py").write_text("hello hidden\n", encoding="utf-8")
    return tmp_path


def test_missing_executables_request_fallback(tree, monkeypatch):
    monkeypatch.setattr(N.shutil, "which", lambda _: None)
    assert N.bfs_files(tree, frozenset()) is None
    assert N.ugrep_search("hello", tree, None, use_regex=False, skip_dirs=frozenset()) is None


def test_bfs_uses_nul_paths_pruning_and_no_shell(tree, monkeypatch):
    monkeypatch.setattr(N.shutil, "which", lambda _: "bfs")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"./src/a.py\0./notes.txt\0")

    monkeypatch.setattr(N.subprocess, "run", run)
    assert N.bfs_files(tree, frozenset({".git"})) == [tree / "src/a.py", tree / "notes.txt"]
    command, options = seen[0]
    assert command[-3:] == ["-type", "f", "-print0"]
    assert ".git" in command and "*.egg-info" in command
    start = command.index(".git") - 5
    assert command[start : start + 7] == ["-exclude", "(", "-type", "d", "-name", ".git", ")"]
    assert options["timeout"] == 20 and options.get("shell", False) is False


@pytest.mark.parametrize("failure", [OSError("missing"), subprocess.TimeoutExpired("bfs", 1), 2])
def test_native_failures_allow_python_fallback(tree, monkeypatch, failure):
    monkeypatch.setattr(N.shutil, "which", lambda _: "native")

    def run(*_, **__):
        if isinstance(failure, Exception):
            raise failure
        return SimpleNamespace(returncode=failure, stdout=b"")

    monkeypatch.setattr(N.subprocess, "run", run)
    assert N.bfs_files(tree, frozenset()) is None
    assert N.ugrep_search("x", tree, None, use_regex=False, skip_dirs=frozenset()) is None


def test_ugrep_json_and_literal_argument_separation(tree, monkeypatch):
    monkeypatch.setattr(N.shutil, "which", lambda _: "ugrep")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"file": "src/a.py", "matches": [{"line": 1, "match": "hello café\n"}]}]
            ),
        )

    monkeypatch.setattr(N.subprocess, "run", run)
    result = N.ugrep_search(
        "-literal.*", tree, "*.py", use_regex=False, skip_dirs=frozenset({".git"})
    )
    assert result == {str((tree / "src/a.py").resolve()): [(1, "hello café")]}
    command, options = seen[0]
    assert command[-3:] == ["--", "-literal.*", "."]
    assert "-F" in command and "-j" in command and "--exclude-dir=.git" in command
    assert options["encoding"] == "utf-8" and options.get("shell", False) is False


@pytest.mark.parametrize("payload", ["bad json", "{}", '[{"file":"a","matches":[{}]}]'])
def test_malformed_native_output_falls_back(tree, monkeypatch, payload):
    monkeypatch.setattr(N.shutil, "which", lambda _: "ugrep")
    monkeypatch.setattr(
        N.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload)
    )
    assert N.ugrep_search("x", tree, None, use_regex=True, skip_dirs=frozenset()) is None


def test_python_search_and_glob_fallbacks(tree, monkeypatch):
    import novacode_cli.backends.filesystem as F

    monkeypatch.setattr(N, "bfs_files", lambda *a, **k: None)
    monkeypatch.setattr(N, "ugrep_search", lambda *a, **k: None)
    monkeypatch.setattr(F, "_resolve_ripgrep_path", lambda: None)
    backend = OptimizedFilesystemBackend(root_dir=tree, virtual_mode=True)
    glob = backend.glob("*.py")
    assert glob.error is None and [item["path"] for item in glob.matches] == ["/src/a.py"]
    found = backend.grep("hello", glob="*.py")
    assert found.error is None and len(found.matches) == 2
    assert {item["path"] for item in found.matches} == {"/src/a.py"}
    assert backend.grep("Hello", glob="*.py").matches[0]["line"] == 2
    assert len(backend.grep(r"h.llo", glob="*.py", use_regex=True).matches) == 2
    assert backend.grep("absent").matches == []
    assert backend.grep("h.llo").matches == [], "literal patterns must not become regexes"
    assert len(backend.grep("hello", path="/notes.txt").matches) == 1
    assert backend.grep("[", use_regex=True).error


def test_native_backend_path_mapping_and_match_cap(tree, monkeypatch):
    monkeypatch.setattr(
        N,
        "ugrep_search",
        lambda *a, **k: {
            str(tree / "src/a.py"): [(1, "hello"), (2, "Hello")],
            str(tree.parent / "outside.py"): [(1, "outside")],
        },
    )
    backend = OptimizedFilesystemBackend(root_dir=tree, virtual_mode=True)
    found, truncated = backend._rg_impl("hello", tree, None, max_count=1)
    assert found == {"/src/a.py": [(1, "hello")]} and truncated


def test_native_timeout_falls_back_through_backend(tree, monkeypatch):
    import novacode_cli.backends.filesystem as F

    monkeypatch.setattr(N.shutil, "which", lambda _: "native")
    monkeypatch.setattr(F, "_resolve_ripgrep_path", lambda: None)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("native", 1)

    monkeypatch.setattr(N.subprocess, "run", timeout)
    backend = OptimizedFilesystemBackend(root_dir=tree, virtual_mode=True)
    assert len(backend.grep("hello", glob="*.py").matches) == 2
    assert [m["path"] for m in backend.glob("*.py").matches] == ["/src/a.py"]


def test_real_bfs_and_ugrep(tree):
    missing = [name for name in ("bfs", "ugrep") if shutil.which(name) is None]
    if missing:
        if os.environ.get("NOVA_REQUIRE_NATIVE_SEARCH") == "1":
            pytest.fail(f"Required native search executables are missing: {missing}")
        pytest.skip(f"Optional native executables unavailable: {missing}")
    files = N.bfs_files(tree, frozenset({".git"}))
    assert files is not None and {p.relative_to(tree).as_posix() for p in files} == {
        "src/a.py",
        "notes.txt",
    }
    found = N.ugrep_search("hello", tree, "*.py", use_regex=False, skip_dirs=frozenset({".git"}))
    assert found == {str((tree / "src/a.py").resolve()): [(1, "hello café"), (2, "Hello again")]}
    backend = OptimizedFilesystemBackend(root_dir=tree, virtual_mode=True)
    assert [m["path"] for m in backend.glob("*.py").matches] == ["/src/a.py"]
    assert len(backend.grep("hello", glob="*.py").matches) == 2


async def test_async_backend_search_uses_same_fallbacks(tree, monkeypatch):
    import novacode_cli.backends.filesystem as F

    monkeypatch.setattr(N, "bfs_files", lambda *a, **k: None)
    monkeypatch.setattr(N, "ugrep_search", lambda *a, **k: None)
    monkeypatch.setattr(F, "_resolve_ripgrep_path", lambda: None)
    backend = OptimizedFilesystemBackend(root_dir=tree, virtual_mode=True)
    assert (await backend.aglob("*.py")).matches
    assert len((await backend.agrep("hello", glob="*.py")).matches) == 2
