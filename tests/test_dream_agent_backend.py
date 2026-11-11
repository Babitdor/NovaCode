"""The dream agent's memory backend must not decode with the locale codec.

Regression: the dream agent served ``/memories/`` with upstream deepagents'
``FilesystemBackend``, whose ripgrep path decodes search output with the
platform locale codec (cp1252 on Windows). Grepping a memory file that contains
a non-ASCII character (a curly quote is ``E2 80 9D``) raised
``UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d`` and stopped the
background dream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from novacode_cli.agents.dream_agent import build_dream_backend
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

if TYPE_CHECKING:
    from pathlib import Path


def test_memory_route_uses_the_optimized_backend(tmp_path: Path) -> None:
    backend = build_dream_backend(tmp_path)
    route = backend.routes["/memories/"]
    assert isinstance(route, OptimizedFilesystemBackend)


def test_memory_route_rejects_all_semantic_writes(tmp_path: Path) -> None:
    (tmp_path / "topic.md").write_text("# Topic\nOriginal.\n", encoding="utf-8")
    backend = build_dream_backend(tmp_path).routes["/memories/"]

    assert backend.write("/topic.md", "Replacement").error
    assert backend.edit("/topic.md", "Original.", "Replacement").error
    assert backend.delete("/topic.md").error
    assert backend.upload_files([("/new.md", b"new")])[0].error
    assert (tmp_path / "topic.md").read_text(encoding="utf-8") == "# Topic\nOriginal.\n"
    assert not (tmp_path / "new.md").exists()


def test_memory_search_hides_other_project_scopes(tmp_path: Path) -> None:
    for project, secret in (("project-a", "ACTIVE_ONLY"), ("project-b", "OTHER_ONLY")):
        page = tmp_path / "memories" / "projects" / project / "notes.md"
        page.parent.mkdir(parents=True)
        page.write_text(f"# {project}\n{secret}\n", encoding="utf-8")
    backend = build_dream_backend(tmp_path, "project-a")

    active = backend.read("/memories/memories/projects/project-a/notes.md")
    other = backend.read("/memories/memories/projects/project-b/notes.md")
    matches = backend.grep("ONLY", path="/memories/")
    files = backend.glob("**/*.md", path="/memories/")
    projects = backend.ls("/memories/memories/projects")

    assert active.file_data is not None
    assert "ACTIVE_ONLY" in active.file_data["content"]
    assert other.error
    assert other.file_data is None
    assert all("OTHER_ONLY" not in match["text"] for match in matches.matches or [])
    assert all("project-b" not in match["path"] for match in files.matches or [])
    assert all("project-b" not in entry["path"] for entry in projects.entries or [])


def test_memory_read_rejects_project_symlink_escape(tmp_path: Path) -> None:
    active = tmp_path / "memories" / "projects" / "project-a"
    other = tmp_path / "memories" / "projects" / "project-b" / "notes.md"
    active.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    other.write_text("Other project memory.", encoding="utf-8")
    link = active / "linked.md"
    try:
        link.symlink_to(other)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    result = build_dream_backend(tmp_path, "project-a").read(
        "/memories/memories/projects/project-a/linked.md"
    )

    assert result.error
    assert result.file_data is None


def test_memory_read_rejects_symlink_outside_agent_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("Outside memory.", encoding="utf-8")
    link = tmp_path / "memories" / "outside.md"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    result = build_dream_backend(tmp_path).read("/memories/memories/outside.md")

    assert result.error
    assert result.file_data is None


def test_grep_over_memory_with_non_ascii_does_not_raise(tmp_path: Path) -> None:
    """Grepping a memory file with a curly quote must return matches, not raise."""
    (tmp_path / "notes.md").write_text(
        "alpha needle omega\nthe \u201dquoted\u201d line has a needle too\n",
        encoding="utf-8",
    )
    backend = build_dream_backend(tmp_path)

    result = backend.grep("needle", path="/memories/")

    assert result.error is None
    assert result.matches, "grep over the memory files found nothing"
    lines = [m["text"] for m in result.matches]
    assert any("\u201dquoted\u201d" in line for line in lines), lines
