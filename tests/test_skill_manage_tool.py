"""Tests for the agent-facing ``skill_manage`` write-path tool.

Covers each sub-operation (create / patch / edit / delete), the create-dedup
guard, and the bundled-skill protection on patch/edit/delete. The LLM authoring
step (``_generate_skill``) is stubbed so no model is invoked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import novacode_cli.config.config as config_mod
import novacode_cli.skills.skill_creation as skill_creation_mod
from novacode_cli.tools.skill_tools import skill_manage
from novacode_cli.hermes.ledger import LearningLedger


def _run(action: str, **kwargs) -> str:
    """Invoke the (async) tool and return its string result."""
    return asyncio.run(skill_manage.ainvoke({"action": action, **kwargs}))


class _FakeSettings:
    """Settings stub pointing user/project/claude skill dirs at tmp locations."""

    _user: Path
    _project: list[Path]
    _claude: Path

    @classmethod
    def from_environment(cls):
        return cls()

    def ensure_user_skills_dir(self, _agent=None):
        self._user.mkdir(parents=True, exist_ok=True)
        return self._user

    def get_project_skills_dirs(self):
        return [p for p in self._project if p.exists()]

    def get_agent_dir(self, _agent=None):
        path = self._user.parent / "agents" / "nova-agent"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def get_global_claude_skills_dir():
        return _FakeSettings._claude


@pytest.fixture
def skill_env(tmp_path, monkeypatch):
    """Wire ``Settings`` to tmp dirs and stub the LLM skill author."""
    user = tmp_path / "user" / "skills"
    project = tmp_path / "proj" / ".nova" / "skills"
    claude = tmp_path / "claude" / "skills"
    for p in (user, project, claude):
        p.mkdir(parents=True, exist_ok=True)

    _FakeSettings._user = user
    _FakeSettings._project = [project]
    _FakeSettings._claude = claude
    monkeypatch.setattr(config_mod, "Settings", _FakeSettings)

    async def _fake_generate(name, base_dir, description=None):
        skill_dir = Path(base_dir) / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            f'---\nname: {name}\ndescription: "{description}"\n---\n\n'
            f"# {name}\n\nStep 1.\n"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return content

    monkeypatch.setattr(skill_creation_mod, "_generate_skill", _fake_generate)
    return {"user": user, "project": project, "claude": claude}


def _write_skill(base: Path, name: str, body: str = "Original step.") -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f'---\nname: {name}\ndescription: "old"\n---\n\n# {name}\n\n{body}\n',
        encoding="utf-8",
    )
    return md


def _proposal() -> object:
    agent_dir = _FakeSettings().get_agent_dir()
    return LearningLedger(agent_dir / "learning.sqlite3").list()[-1]


# ── create ──────────────────────────────────────────────────────────────────


def test_create_stages_skill(skill_env):
    result = _run("create", name="add-tui-command", description="Use when adding a /command.")
    assert "Proposed skill" in result
    assert not (skill_env["user"] / "add-tui-command" / "SKILL.md").exists()
    assert _proposal().target == "add-tui-command"


def test_create_requires_description(skill_env):
    result = _run("create", name="no-desc")
    assert "description" in result.lower()
    assert not (skill_env["user"] / "no-desc").exists()


def test_create_dedup(skill_env):
    _write_skill(skill_env["user"], "existing-skill")
    result = _run("create", name="existing-skill", description="anything")
    assert "already exists" in result


def test_create_slugifies_name(skill_env):
    _run("create", name="My Cool Skill!", description="trigger")
    assert _proposal().target == "my-cool-skill"


# ── patch ───────────────────────────────────────────────────────────────────


def test_patch_unique_replacement(skill_env):
    _write_skill(skill_env["user"], "patch-me", body="findme here")
    result = _run("patch", name="patch-me", old="findme", new="replaced")
    assert "Proposed patch" in result
    content = (skill_env["user"] / "patch-me" / "SKILL.md").read_text(encoding="utf-8")
    assert "findme here" in content
    assert "replaced here" in _proposal().content


def test_patch_missing_text(skill_env):
    _write_skill(skill_env["user"], "patch-me")
    result = _run("patch", name="patch-me", old="absent", new="x")
    assert "not found" in result


def test_patch_ambiguous_text(skill_env):
    _write_skill(skill_env["user"], "patch-me", body="dup dup")
    result = _run("patch", name="patch-me", old="dup", new="x")
    assert "appears" in result and "unique" in result


def test_patch_nonexistent(skill_env):
    result = _run("patch", name="ghost", old="a", new="b")
    assert "no skill" in result.lower()


# ── edit ────────────────────────────────────────────────────────────────────


def test_edit_rewrites_body_preserving_frontmatter(skill_env):
    _write_skill(skill_env["user"], "edit-me")
    result = _run("edit", name="edit-me", body="# New\n\nBrand new body.")
    assert "Proposed edit" in result
    content = (skill_env["user"] / "edit-me" / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "Original step." in content
    assert "Brand new body." in _proposal().content


def test_edit_updates_description(skill_env):
    _write_skill(skill_env["user"], "edit-me")
    _run("edit", name="edit-me", body="body", description="brand new trigger")
    content = _proposal().content
    assert "brand new trigger" in content


def test_edit_requires_body(skill_env):
    _write_skill(skill_env["user"], "edit-me")
    result = _run("edit", name="edit-me")
    assert "body" in result.lower()


# ── delete ──────────────────────────────────────────────────────────────────


def test_delete_user_skill(skill_env):
    _write_skill(skill_env["user"], "delete-me")
    result = _run("delete", name="delete-me")
    # Delete is now a soft-delete (archived, recoverable).
    assert "Proposed recoverable archive" in result
    assert (skill_env["user"] / "delete-me").exists()
    assert _proposal().kind == "skill_archive"


def test_delete_project_skill(skill_env):
    _write_skill(skill_env["project"], "proj-skill")
    result = _run("delete", name="proj-skill")
    assert "Proposed recoverable archive" in result
    assert (skill_env["project"] / "proj-skill").exists()


def test_delete_nonexistent(skill_env):
    result = _run("delete", name="ghost")
    assert "no skill" in result.lower()


# ── bundled-skill protection ────────────────────────────────────────────────


def test_cannot_delete_bundled_claude_skill(skill_env):
    _write_skill(skill_env["claude"], "bundled")
    result = _run("delete", name="bundled")
    assert "bundled" in result.lower() and "cannot" in result.lower()
    assert (skill_env["claude"] / "bundled").exists()


def test_cannot_patch_bundled_claude_skill(skill_env):
    _write_skill(skill_env["claude"], "bundled", body="findme")
    result = _run("patch", name="bundled", old="findme", new="x")
    assert "cannot be modified" in result
    content = (skill_env["claude"] / "bundled" / "SKILL.md").read_text(encoding="utf-8")
    assert "findme" in content


def test_cannot_edit_bundled_claude_skill(skill_env):
    _write_skill(skill_env["claude"], "bundled")
    result = _run("edit", name="bundled", body="new")
    assert "cannot be modified" in result


# ── misc ────────────────────────────────────────────────────────────────────


def test_unknown_action(skill_env):
    result = _run("frobnicate", name="x")
    assert "unknown action" in result


def test_blank_name(skill_env):
    result = _run("create", name="!!!", description="x")
    assert "valid kebab-case 'name'" in result
