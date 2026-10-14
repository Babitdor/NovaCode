"""Plan routes must not create empty project directories during agent setup."""

from __future__ import annotations

from pathlib import Path

from novacode_cli.agents.core_agent import _build_composite_backend


def test_plan_route_creates_project_directory_only_when_written(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    backend, _ = _build_composite_backend(
        sandbox=None,
        sandbox_type=None,
        workspace_root=project,
        skills_dir=tmp_path / "skills",
        claude_skills_dir=tmp_path / "claude-skills",
        project_skills_dirs=[],
        plugin_skills=[],
        agent_dir=None,
        store=None,
        assistant_id="nova-agent",
        session_id="test-lazy-plans",
    )

    assert not (project / ".nova").exists()
    result = backend.write("/.nova/plans/plan-example.md", "# Example\n")
    assert result.error is None
    assert (project / ".nova" / "plans" / "plan-example.md").read_text(
        encoding="utf-8"
    ) == "# Example\n"
