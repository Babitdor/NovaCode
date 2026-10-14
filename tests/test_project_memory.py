"""Project memory (NOVA.md/CLAUDE.md) must load into <project_memory> every
session — including resumed ones.

Regression: on resume the middleware was created with skip_project_memory=True
and NOVA.md was only seeded into the first continuation message (which
summarization later evicts), so <project_memory> showed "(No project ...)".
"""

from __future__ import annotations

from pathlib import Path

from novacode_cli.config.config import _find_project_agent_md
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware


class _FakeSettings:
    def __init__(self, project_root, agent_dir):
        self.project_root = project_root
        self._agent_dir = agent_dir

    def get_agent_dir(self, _aid):
        return self._agent_dir

    def get_user_agent_md_path(self, _aid):
        return self._agent_dir / "agent.md"

    def get_project_agent_md_paths(self):
        # Mirrors _find_project_agent_md: only existing files.
        candidates = [
            self.project_root / "NOVA.md",
            self.project_root / ".nova" / "NOVA.md",
            self.project_root / "CLAUDE.md",
            self.project_root / "AGENTS.md",
        ]
        return [p for p in candidates if p.exists()]


def _make(tmp_path, *, skip):
    nova_dir = tmp_path / ".nova"
    nova_dir.mkdir()
    (nova_dir / "NOVA.md").write_text("# Project Rules\nUse pytest.\n", encoding="utf-8")
    agent_dir = tmp_path / "agentdir"
    agent_dir.mkdir()
    settings = _FakeSettings(tmp_path, agent_dir)
    return AgentMemoryMiddleware(
        settings=settings, assistant_id="nova-agent", skip_project_memory=skip
    )


def test_nova_md_in_dot_nova_is_loaded(tmp_path):
    mw = _make(tmp_path, skip=False)
    update = mw.before_agent({})
    assert "project_memory" in update
    assert "Use pytest." in update["project_memory"]


def test_skip_flag_still_skips(tmp_path):
    mw = _make(tmp_path, skip=True)
    update = mw.before_agent({})
    assert "project_memory" not in update


def test_agents_md_is_loaded_with_existing_project_instructions(tmp_path: Path) -> None:
    mw = _make(tmp_path, skip=False)
    (tmp_path / "AGENTS.md").write_text("# Agent rules\nRun focused tests.\n", encoding="utf-8")

    assert [path.name for path in _find_project_agent_md(tmp_path)] == [
        "NOVA.md",
        "AGENTS.md",
    ]
    update = mw.before_agent({})
    assert "Use pytest." in update["project_memory"]
    assert "Run focused tests." in update["project_memory"]
