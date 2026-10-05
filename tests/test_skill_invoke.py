"""Tests for the presentation-free skill resolver.

``_try_skill_invocation`` now returns a ``SkillInvocation`` (prompt + metadata)
and prints nothing — each UI renders the metadata itself (legacy REPL via rich,
TUI via native widgets).
"""

import asyncio
from pathlib import Path

import novacode_cli.commands.skill_invoke as si
from novacode_cli.commands.skill_invoke import SkillInvocation, _try_skill_invocation


class _FakeSettings:
    project_root = None

    @staticmethod
    def from_environment():
        return _FakeSettings()

    def ensure_user_skills_dir(self, _assistant_id=None):
        return Path(".")

    @staticmethod
    def get_shared_skills_dir():
        return Path("no-such-shared-skills-dir-xyz")

    def get_project_skills_dirs(self):
        return []

    def get_project_skills_dir(self):
        return None

    @staticmethod
    def get_global_claude_skills_dir():
        return Path("no-such-claude-skills-dir-xyz")


def _patch_common(monkeypatch, skills):
    monkeypatch.setattr(si, "Settings", _FakeSettings)
    monkeypatch.setattr(si, "list_skills", lambda **k: skills)
    monkeypatch.setattr(si, "_get_supporting_files", lambda d: {})


def test_resolver_returns_metadata_and_prompt(monkeypatch):
    _patch_common(
        monkeypatch,
        [{"name": "graphify", "description": "Turn input into a graph", "source": "global", "path": ""}],
    )
    monkeypatch.setattr(si, "_read_skill_content", lambda *a, **k: "# Graphify\nbody")

    res = asyncio.run(_try_skill_invocation("graphify", "x.py", object(), "nova"))

    assert isinstance(res, SkillInvocation)
    assert res.name == "graphify"
    assert res.source == "global"
    assert res.args == "x.py"
    assert res.supporting_files == []
    # Prompt embeds the skill body and the user-provided args.
    assert "# Graphify" in res.prompt
    assert "User provided additional context: x.py" in res.prompt


def test_resolver_matches_underscores_to_hyphens(monkeypatch):
    _patch_common(
        monkeypatch,
        [{"name": "api-testing", "description": "d", "source": "project", "path": ""}],
    )
    monkeypatch.setattr(si, "_read_skill_content", lambda *a, **k: "body")

    # User typed an underscore; skill name uses a hyphen.
    res = asyncio.run(_try_skill_invocation("api_testing", None, object(), "nova"))
    assert res is not None
    assert res.name == "api-testing"
    assert res.source == "project"


def test_resolver_returns_none_for_unknown(monkeypatch):
    _patch_common(monkeypatch, [])
    res = asyncio.run(_try_skill_invocation("nope", None, object(), "nova"))
    assert res is None


def test_resolver_returns_none_for_empty_skill_md(monkeypatch):
    _patch_common(
        monkeypatch,
        [{"name": "empty", "description": "d", "source": "global", "path": ""}],
    )
    monkeypatch.setattr(si, "_read_skill_content", lambda *a, **k: None)
    res = asyncio.run(_try_skill_invocation("empty", None, object(), "nova"))
    assert res is None


def test_resolver_module_is_presentation_free():
    # The resolver no longer pulls in the rich console / COLORS — rendering lives
    # in the callers (commands.py legacy, NovaApp._run_skill in the TUI).
    assert not hasattr(si, "console")
    assert not hasattr(si, "COLORS")
