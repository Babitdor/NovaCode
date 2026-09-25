"""Tests for skill curation — prefs persistence and the clamping middleware.

Pins the opt-out model (a skill is on unless disabled), the cross-scope
union (global OR project), and that the middleware drops the disabled skills from
``skills_metadata`` while leaving an empty/clean list untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from novacode_cli.skills import skills_prefs as sp
from novacode_cli.skills.curation_middleware import SkillCurationMiddleware

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ── core load / save ─────────────────────────────────────────────────────────


def test_load_missing_file_is_empty(tmp_path: Path):
    assert sp.load_disabled(tmp_path / "nope.json") == set()
    assert sp.load_disabled(None) == set()


def test_save_then_load_roundtrip(tmp_path: Path):
    path = tmp_path / "skills_prefs.json"
    sp.save_disabled(path, ["b", "a", "a"])  # duplicates collapse
    assert sp.load_disabled(path) == {"a", "b"}


def test_save_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "skills_prefs.json"
    sp.save_disabled(path, {"x"})
    assert path.exists()
    assert sp.load_disabled(path) == {"x"}


def test_load_tolerates_garbage(tmp_path: Path):
    path = tmp_path / "skills_prefs.json"
    path.write_text("not json{", encoding="utf-8")
    assert sp.load_disabled(path) == set()
    path.write_text('{"disabled": "notalist"}', encoding="utf-8")
    assert sp.load_disabled(path) == set()
    path.write_text("[1, 2, 3]", encoding="utf-8")  # top-level not a dict
    assert sp.load_disabled(path) == set()


# ── effective union across scopes ────────────────────────────────────────────


def test_effective_disabled_is_union(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    g = tmp_path / "global.json"
    p = tmp_path / "project.json"
    sp.save_disabled(g, {"a", "b"})
    sp.save_disabled(p, {"b", "c"})
    monkeypatch.setattr(sp, "global_prefs_path", lambda: g)
    monkeypatch.setattr(sp, "project_prefs_path", lambda: p)
    assert sp.effective_disabled() == {"a", "b", "c"}
    assert sp.is_skill_enabled("d") is True
    assert sp.is_skill_enabled("a") is False


def test_set_skill_enabled_toggles_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    g = tmp_path / "global.json"
    monkeypatch.setattr(sp, "global_prefs_path", lambda: g)
    monkeypatch.setattr(sp, "scope_path", lambda scope: g if scope == "global" else None)

    sp.set_skill_enabled("docx", enabled=False, scope="global")
    assert sp.load_disabled(g) == {"docx"}
    # Re-enabling removes it again (opt-out default restored).
    sp.set_skill_enabled("docx", enabled=True, scope="global")
    assert sp.load_disabled(g) == set()


def test_set_project_scope_without_project_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sp, "scope_path", lambda _scope: None)
    try:
        sp.set_skill_enabled("x", enabled=False, scope="project")
    except ValueError:
        return
    msg = "expected ValueError when no project is available"
    raise AssertionError(msg)


def test_signature_changes_when_prefs_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    g = tmp_path / "global.json"
    monkeypatch.setattr(sp, "global_prefs_path", lambda: g)
    monkeypatch.setattr(sp, "project_prefs_path", lambda: None)
    before = sp.prefs_signature()
    sp.save_disabled(g, {"a"})
    assert sp.prefs_signature() != before


# ── middleware clamping ──────────────────────────────────────────────────────


def _skill(name: str) -> dict:
    return {"name": name, "description": f"{name} skill", "path": f"/{name}/SKILL.md"}


def _run(mw: SkillCurationMiddleware, skills: list[dict] | None) -> dict | None:
    state = {} if skills is None else {"skills_metadata": skills}
    return mw.before_agent(state, SimpleNamespace(), {})


def test_clamp_drops_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "novacode_cli.skills.curation_middleware.effective_disabled",
        lambda: {"b"},
    )
    out = _run(SkillCurationMiddleware(), [_skill("a"), _skill("b"), _skill("c")])
    assert [s["name"] for s in out["skills_metadata"]] == ["a", "c"]


def test_clamp_noop_when_nothing_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "novacode_cli.skills.curation_middleware.effective_disabled",
        set,
    )
    assert _run(SkillCurationMiddleware(), [_skill("a")]) is None


def test_clamp_noop_when_no_skills_loaded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "novacode_cli.skills.curation_middleware.effective_disabled",
        lambda: {"a"},
    )
    assert _run(SkillCurationMiddleware(), None) is None
    assert _run(SkillCurationMiddleware(), []) is None


def test_clamp_noop_when_disabled_set_misses(monkeypatch: pytest.MonkeyPatch):
    # Disabled names that aren't loaded must not trigger a (wasteful) update.
    monkeypatch.setattr(
        "novacode_cli.skills.curation_middleware.effective_disabled",
        lambda: {"zzz"},
    )
    assert _run(SkillCurationMiddleware(), [_skill("a"), _skill("b")]) is None


async def test_async_clamp_matches_sync(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "novacode_cli.skills.curation_middleware.effective_disabled",
        lambda: {"a"},
    )
    mw = SkillCurationMiddleware()
    state = {"skills_metadata": [_skill("a"), _skill("b")]}
    out = await mw.abefore_agent(state, SimpleNamespace(), {})
    assert [s["name"] for s in out["skills_metadata"]] == ["b"]


# ── /skills console entry ────────────────────────────────────────────────────


def test_console_skills_command_points_at_the_tui():
    """The console /skills stub must not prompt — the TUI owns the menus."""
    import asyncio
    import inspect

    from novacode_cli.commands import skills_commands

    src = inspect.getsource(skills_commands)
    # Check for real usage, not the docstring's prose mention.
    assert "import PromptSession" not in src
    assert "run_interactive_menu" not in src
    assert "prompt_async" not in src
    # It stays registered and is a no-op handler that reports where to go.
    assert asyncio.run(skills_commands.handle_skills_command(None, "nova-agent")) is True
