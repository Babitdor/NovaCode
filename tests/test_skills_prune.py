"""Tests for skill pruning — archive, restore, and the bundled-skill guard.

Pruning is a **soft delete**: `archive_many` moves each skill directory to the
sibling `skills-archive/`, so every removal is recoverable through
`restore_skill`. These tests pin that round trip, the refusal to touch bundled
(Claude) skills, the refusal to clobber a live skill on restore, and backwards
compatibility with archives written before `origin.json` existed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from novacode_cli.skills import prune, versioning


def _write_skill(base: Path, name: str, description: str = "a trigger") -> Path:
    """Create ``<base>/<name>/SKILL.md`` and return the skill directory."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\n# {name}\n\nStep 1.\n',
        encoding="utf-8",
    )
    return d


@pytest.fixture
def skill_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Point Settings at tmp user/project/claude skill dirs.

    Mirrors the harness in ``test_skill_manage_tool.py`` so prune sees the same
    directory layout the real app does.
    """
    import novacode_cli.config.config as config_mod

    user = tmp_path / "user" / "skills"
    project = tmp_path / "proj" / ".nova" / "skills"
    claude = tmp_path / "claude" / "skills"
    for p in (user, project, claude):
        p.mkdir(parents=True, exist_ok=True)

    class _FakeSettings:
        @classmethod
        def from_environment(cls) -> _FakeSettings:
            return cls()

        def get_global_skills_dir(self) -> Path:
            return user

        def ensure_user_skills_dir(self, _agent: object = None) -> Path:
            user.mkdir(parents=True, exist_ok=True)
            return user

        def get_project_skills_dirs(self) -> list[Path]:
            return [project]

        def get_project_skills_dir(self) -> Path:
            return project

        @staticmethod
        def get_global_claude_skills_dir() -> Path:
            return claude

    monkeypatch.setattr(config_mod, "Settings", _FakeSettings)

    # ``skills_prefs`` reads the module-level ``settings`` singleton for its
    # paths, so patch that too — otherwise a prune would rewrite the real
    # ~/.nova/skills_prefs.json from inside a test.
    import novacode_cli.skills.skills_prefs as prefs_mod

    class _FakePrefsSettings:
        user_deepagents_dir = user.parent
        project_root = None

    monkeypatch.setattr(prefs_mod, "settings", _FakePrefsSettings())
    return {"user": user, "project": project, "claude": claude}


# ── candidates ───────────────────────────────────────────────────────────────


def test_candidates_lists_skills_with_source_and_prunability(skill_dirs: dict) -> None:
    _write_skill(skill_dirs["user"], "mine", "user trigger")
    _write_skill(skill_dirs["project"], "proj-skill", "project trigger")
    _write_skill(skill_dirs["claude"], "bundled", "claude trigger")

    found = {c["name"]: c for c in prune.candidates()}

    assert found["mine"]["source"] == "user"
    assert found["mine"]["prunable"] is True
    assert found["proj-skill"]["source"] == "project"
    assert found["proj-skill"]["prunable"] is True
    # Bundled skills are listed but never prunable.
    assert found["bundled"]["source"] == "claude"
    assert found["bundled"]["prunable"] is False


def test_candidates_reads_description_and_ignores_dot_dirs(skill_dirs: dict) -> None:
    _write_skill(skill_dirs["user"], "described", "when you need X")
    # A dot-dir (version history) must not be mistaken for a skill.
    _write_skill(skill_dirs["user"] / ".history", "hidden", "nope")

    found = {c["name"]: c for c in prune.candidates()}

    assert found["described"]["description"] == "when you need X"
    assert "hidden" not in found


def test_candidates_reports_usage_when_supplied(skill_dirs: dict) -> None:
    _write_skill(skill_dirs["user"], "used")
    found = {c["name"]: c for c in prune.candidates({"used": {"invocations": 7}})}
    assert found["used"]["invocations"] == 7


def test_candidates_defaults_usage_to_zero_when_learning_is_off(skill_dirs: dict) -> None:
    """The Hermes store is opt-in; absent counts must read 0, not 'unused'."""
    _write_skill(skill_dirs["user"], "never-counted")
    found = {c["name"]: c for c in prune.candidates()}
    assert found["never-counted"]["invocations"] == 0


# ── archive / restore round trip ─────────────────────────────────────────────


def test_archive_then_restore_round_trip(skill_dirs: dict) -> None:
    skill_dir = _write_skill(skill_dirs["user"], "temp")

    ok, msg = prune.archive_one("temp")
    assert ok, msg
    assert not skill_dir.exists(), "skill dir should be gone from the live tree"

    archived = prune.list_archived_skills()
    assert [a["name"] for a in archived] == ["temp"]

    ok, msg = prune.restore_skill(archived[0]["path"])
    assert ok, msg
    assert (skill_dir / "SKILL.md").is_file(), "SKILL.md should be back"


def test_archive_moves_outside_the_skills_root(skill_dirs: dict) -> None:
    """The archive must not sit inside a scanned dir, or it would be listed."""
    _write_skill(skill_dirs["user"], "gone")
    ok, _ = prune.archive_one("gone")
    assert ok

    archive_root = prune.archive_root_for(skill_dirs["user"])
    assert archive_root.exists()
    assert skill_dirs["user"] not in archive_root.parents


def test_archive_many_reports_per_skill_outcomes(skill_dirs: dict) -> None:
    _write_skill(skill_dirs["user"], "real")

    results = prune.archive_many(["real", "never-existed"])
    by_name = {name: (ok, msg) for name, ok, msg in results}

    assert by_name["real"][0] is True
    assert by_name["never-existed"][0] is False
    assert "no skill named" in by_name["never-existed"][1]


def test_archive_removes_the_skill_lock_entry(skill_dirs: dict) -> None:
    """Mirrors skill_manage delete: a pruned skill must not linger in the lock."""
    from novacode_cli.skills.skill_lock import SkillLock

    _write_skill(skill_dirs["user"], "locked")
    lock = SkillLock.for_skills_dir(skill_dirs["user"])
    lock.update("locked", {"installed": True})
    assert lock.get("locked") is not None

    ok, _ = prune.archive_one("locked")
    assert ok
    assert SkillLock.for_skills_dir(skill_dirs["user"]).get("locked") is None


def test_archive_clears_a_stale_disabled_pref(skill_dirs: dict) -> None:
    """A disabled-then-pruned skill must not leave a dangling prefs entry."""
    from novacode_cli.skills import skills_prefs

    _write_skill(skill_dirs["user"], "disabled-then-gone")
    prefs_path = skill_dirs["user"].parent / "skills_prefs.json"
    skills_prefs.save_disabled(prefs_path, {"disabled-then-gone"})

    ok, _ = prune.archive_one("disabled-then-gone")
    assert ok
    assert "disabled-then-gone" not in skills_prefs.load_disabled(prefs_path)


# ── guards ───────────────────────────────────────────────────────────────────


def test_bundled_skill_is_never_archived(skill_dirs: dict) -> None:
    bundled = _write_skill(skill_dirs["claude"], "sacred")

    ok, msg = prune.archive_one("sacred")

    assert ok is False
    assert "bundled" in msg
    assert (bundled / "SKILL.md").is_file(), "bundled skill must be untouched"


def test_restore_refuses_to_clobber_a_live_skill(skill_dirs: dict) -> None:
    _write_skill(skill_dirs["user"], "dup")
    ok, _ = prune.archive_one("dup")
    assert ok
    archived = prune.list_archived_skills()

    # A new skill with the same name appears before we restore.
    _write_skill(skill_dirs["user"], "dup")

    ok, msg = prune.restore_skill(archived[0]["path"])
    assert ok is False
    assert "already exists" in msg


def test_archive_one_on_missing_skill_is_a_clean_failure() -> None:
    ok, msg = prune.archive_one("nope")
    assert ok is False
    assert "no skill named" in msg


def test_list_archived_skills_is_empty_without_an_archive(skill_dirs: dict) -> None:
    # No archive directory exists yet for either scope.
    assert not prune.archive_root_for(skill_dirs["user"]).exists()
    assert prune.list_archived_skills() == []


# ── backwards compatibility ──────────────────────────────────────────────────


def test_restore_works_for_a_legacy_archive_without_origin_json(skill_dirs: dict) -> None:
    """Archives written before origin.json existed must still restore."""
    archive_root = prune.archive_root_for(skill_dirs["user"])
    legacy = archive_root / "old-skill-20260101T000000_000000_0000"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: old-skill\n---\nbody", encoding="utf-8")
    assert not (legacy / "origin.json").exists()

    listed = prune.list_archived_skills()
    assert [a["name"] for a in listed] == ["old-skill"]

    ok, msg = prune.restore_skill(listed[0]["path"])
    assert ok, msg
    assert (skill_dirs["user"] / "old-skill" / "SKILL.md").is_file()


def test_legacy_archive_without_the_tiebreaker_counter_parses(skill_dirs: dict) -> None:
    """Older archives are ``<name>-<ts>`` with no trailing ``_NNNN``.

    Both formats exist on real installs, so name recovery must handle each or a
    restore would land under the timestamp as the skill name.
    """
    archive_root = prune.archive_root_for(skill_dirs["user"])
    for dirname in (
        "old-style-20260703T010541_005928",
        "new-style-20260919T122428_562798_0000",
    ):
        d = archive_root / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody", encoding="utf-8")

    names = sorted(a["name"] for a in prune.list_archived_skills())
    assert names == ["new-style", "old-style"]


def test_origin_json_is_written_and_removed_on_restore(skill_dirs: dict) -> None:
    skill_dir = _write_skill(skill_dirs["user"], "origin-test")
    ok, _ = prune.archive_one("origin-test")
    assert ok

    archive_dir = prune.list_archived_skills()[0]["path"]
    origin = json.loads((archive_dir / "origin.json").read_text(encoding="utf-8"))
    assert origin["name"] == "origin-test"
    assert Path(origin["origin"]).parent == skill_dirs["user"]

    ok, _ = prune.restore_skill(archive_dir)
    assert ok
    # The marker describes the archive, not the live skill.
    assert not (skill_dir / "origin.json").exists()


def test_archive_skill_signature_is_unchanged_for_existing_callers(tmp_path: Path) -> None:
    """skill_manage and the hermes curator call archive_skill(skill_dir)."""
    d = _write_skill(tmp_path / "skills", "s")
    dest = versioning.archive_skill(d)
    assert dest is not None
    assert dest.exists()
    assert versioning.list_archived(tmp_path / "skills-archive")[0]["name"] == "s"


# ── TUI wiring ───────────────────────────────────────────────────────────────


def test_skills_screen_prunes_a_marked_skill(skill_dirs: dict) -> None:
    """The TUI Prune flow archives a marked skill and refreshes its caches."""
    try:
        import textual  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("textual not installed")

    from textual.app import App, ComposeResult

    from novacode_cli.tui.screens import SkillsScreen

    _write_skill(skill_dirs["user"], "tui-victim")
    _write_skill(skill_dirs["user"], "tui-keeper")

    class Host(App):
        assistant_id = "nova-agent"

        def __init__(self) -> None:
            super().__init__()
            self.logged: list = []

        def compose(self) -> ComposeResult:
            return []

        def _get_skill_names(self) -> list[str]:
            return ["tui-keeper", "tui-victim"]

        def _log(self, text: object) -> None:
            self.logged.append(text)

        def _refresh_status(self) -> None:
            pass

    async def drive() -> Host:
        app = Host()
        async with app.run_test() as pilot:
            screen = SkillsScreen()
            app.push_screen(screen)
            for _ in range(4):
                await pilot.pause()

            # Enter prune mode, mark the victim, archive it.
            screen._enter_prune()
            await pilot.pause()
            screen._marked = {"tui-victim"}
            screen._archive_marked()
            await pilot.pause()
        return app

    app = asyncio.run(drive())

    assert not (skill_dirs["user"] / "tui-victim").exists()
    assert (skill_dirs["user"] / "tui-keeper" / "SKILL.md").is_file()
    assert any("tui-victim" in str(t) for t in app.logged)
