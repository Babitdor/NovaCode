"""A grep/glob with no path searches the project, not every mounted drive.

Since drive roots were mounted as routes (real absolute host paths), deepagents'
CompositeBackend fanned every path-less search out to them: grep walked all of
B:\\ and C:\\ and timed out ("Grep of 'B:\\' timed out after 30s"), and one
route's timeout failed the whole search. Seen in saved sessions on 09-20/21.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import novacode_cli.agents.core_agent as core_agent


@pytest.fixture()
def composite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "elsewhere.py").write_text("NEEDLE = 2\n", encoding="utf-8")
    drive_root = drive.as_posix() + "/"
    monkeypatch.setattr(core_agent, "_host_drive_roots", lambda _root: [drive_root])
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    backend, _ = core_agent._build_composite_backend(
        sandbox=None,
        sandbox_type=None,
        workspace_root=project,
        skills_dir=tmp_path / "skills",
        claude_skills_dir=tmp_path / "claude-skills",
        project_skills_dirs=[],
        plugin_skills=[],
        agent_dir=None,
        store=None,
        assistant_id="a",
        session_id="s",
    )
    return backend, drive_root


def test_pathless_grep_does_not_walk_mounted_drives(composite) -> None:
    backend, _ = composite
    result = backend.grep("NEEDLE")
    paths = [m["path"] for m in result.matches or []]
    assert any(p.endswith("app.py") for p in paths), paths
    assert not any("elsewhere" in p for p in paths), "searched a whole drive"


def test_pathless_glob_does_not_walk_mounted_drives(composite) -> None:
    backend, _ = composite
    result = backend.glob("**/*.py")
    paths = [m["path"] for m in result.matches or []]
    assert not any("elsewhere" in p for p in paths), "searched a whole drive"


def test_an_absolute_path_on_a_drive_still_resolves(composite) -> None:
    backend, drive_root = composite
    read = backend.read(drive_root + "elsewhere.py")
    assert read.error is None and "NEEDLE = 2" in read.file_data["content"]
    grep = backend.grep("NEEDLE", path=drive_root)
    assert any("elsewhere" in m["path"] for m in grep.matches or [])
