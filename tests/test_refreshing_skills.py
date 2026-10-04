"""Tests for RefreshingSkillsMiddleware mid-session skill refresh."""

from __future__ import annotations

import os
import shutil
import time

from novacode_cli.backends import OptimizedFilesystemBackend as FilesystemBackend
from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware


def _write_skill(skills_dir, name, desc="does things") -> None:  # noqa: ANN001
    sk = skills_dir / name
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n", encoding="utf-8"
    )


def _make(skills_dir):  # noqa: ANN001, ANN202
    backend = FilesystemBackend(root_dir=str(skills_dir), virtual_mode=True)
    return RefreshingSkillsMiddleware(backend=backend, sources=["."], watch_dirs=[skills_dir])


def test_skills_changed_first_call_then_stable(tmp_path):  # noqa: ANN001
    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    assert mw._skills_changed() is True  # first call establishes the baseline
    assert mw._skills_changed() is False  # nothing changed since


def test_skills_changed_detects_add(tmp_path):  # noqa: ANN001
    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    mw._skills_changed()  # baseline
    _write_skill(tmp_path, "beta")
    assert mw._skills_changed() is True


def test_skills_changed_detects_remove(tmp_path):  # noqa: ANN001
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "beta")
    mw = _make(tmp_path)
    mw._skills_changed()  # baseline
    shutil.rmtree(tmp_path / "beta")
    assert mw._skills_changed() is True


def test_skills_changed_detects_edit(tmp_path):  # noqa: ANN001
    _write_skill(tmp_path, "alpha", desc="v1")
    mw = _make(tmp_path)
    mw._skills_changed()  # baseline
    p = tmp_path / "alpha" / "SKILL.md"
    p.write_text(p.read_text(encoding="utf-8").replace("v1", "v2"), encoding="utf-8")
    future = time.time() + 10
    os.utime(p, (future, future))  # ensure a distinct mtime
    assert mw._skills_changed() is True


def _names(mw) -> set[str]:  # noqa: ANN001
    return {s["name"] for s in mw._skills}


def test_skills_are_held_on_the_instance_not_in_state(tmp_path):  # noqa: ANN001
    """State is serialized into every checkpoint; the skill list was most of it."""
    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    upd = mw.before_agent({}, None, None)
    assert _names(mw) == {"alpha"}
    assert "skills_metadata" not in (upd or {}), "nothing skill-shaped goes into state"


def test_a_stale_list_in_state_is_blanked(tmp_path):  # noqa: ANN001
    """Threads saved before the change stop paying for the list too."""
    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    upd = mw.before_agent({"skills_metadata": [{"name": "old"}]}, None, None)
    assert upd == {"skills_metadata": []}


def test_before_agent_does_not_reload_when_unchanged(tmp_path, monkeypatch):  # noqa: ANN001
    from deepagents.middleware.skills import SkillsMiddleware

    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    mw.before_agent({}, None, None)
    calls = []
    real = SkillsMiddleware.before_agent
    monkeypatch.setattr(
        SkillsMiddleware, "before_agent", lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    )
    mw.before_agent({}, None, None)
    assert not calls, "an unchanged skill tree must not be re-parsed every turn"


def test_before_agent_refreshes_after_new_skill(tmp_path):  # noqa: ANN001
    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    mw.before_agent({}, None, None)
    _write_skill(tmp_path, "beta")
    mw.before_agent({}, None, None)
    assert _names(mw) == {"alpha", "beta"}


async def test_abefore_agent_refreshes_after_new_skill(tmp_path):  # noqa: ANN001
    _write_skill(tmp_path, "alpha")
    mw = _make(tmp_path)
    await mw.abefore_agent({}, None, None)
    _write_skill(tmp_path, "beta")
    await mw.abefore_agent({}, None, None)
    assert "beta" in _names(mw)
