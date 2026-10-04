"""Global plan archive: ~/.nova/plans/<project>/ mirrors every approved plan.

Plans live with the project that owns them, so browsing across checkouts was
impossible. The archive is a mirror for that; the project copy stays
authoritative and archiving must never affect a plan approval.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import novacode_cli.plan_archive as pa
import novacode_cli.tools.plan_mode_tools as pmt
from novacode_cli.config.config import settings


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project root with the archive redirected under tmp_path."""
    proj = tmp_path / "myproject"
    proj.mkdir()
    archive = tmp_path / "home" / ".nova" / "plans"
    monkeypatch.setattr(pa, "global_plans_root", lambda: archive)
    monkeypatch.setattr(settings, "project_root", proj, raising=False)
    monkeypatch.chdir(proj)
    return proj, archive


def test_approved_plan_is_mirrored_to_the_archive(project):
    proj, archive = project
    pmt._persist_approved_plan("# Refactor auth flow\n\nStep one.")

    # Project copy is authoritative...
    assert (proj / ".nova" / "plans" / "plan-refactor-auth-flow.md").exists()
    # ...and the archive has a copy, namespaced by project.
    copies = list(archive.rglob("*.md"))
    assert len(copies) == 1
    assert copies[0].name == "plan-refactor-auth-flow.md"
    assert copies[0].parent.name.startswith("myproject-")


def test_listing_reports_title_and_origin(project):
    proj, _ = project
    pmt._persist_approved_plan("# Refactor auth flow\n\nStep one.")

    plans = pa.list_archived_plans()
    assert len(plans) == 1
    assert plans[0].title == "Refactor auth flow"
    assert plans[0].project == str(proj)


def test_same_named_projects_do_not_collide(tmp_path, monkeypatch):
    """Two checkouts both called 'api' must not share an archive directory."""
    archive = tmp_path / "home" / ".nova" / "plans"
    monkeypatch.setattr(pa, "global_plans_root", lambda: archive)

    slugs = set()
    for parent in ("work", "personal"):
        root = tmp_path / parent / "api"
        root.mkdir(parents=True)
        slugs.add(pa.project_slug(root))
    assert len(slugs) == 2, f"slugs collided: {slugs}"


def test_archiving_failure_never_breaks_approval(project, monkeypatch):
    """The project copy is what matters; a broken archive must be survivable."""
    proj, _ = project

    def boom(*_a, **_k):
        raise OSError("archive volume gone")

    monkeypatch.setattr(pa, "mirror_plan", boom)
    saved = pmt._persist_approved_plan("# Still saved\n\nbody")
    assert saved, "approval must still persist the plan"
    assert (proj / ".nova" / "plans" / "plan-still-saved.md").exists()


def test_backfill_picks_up_preexisting_plans(project):
    """Plans written before the archive existed are otherwise invisible."""
    proj, archive = project
    plans_dir = proj / ".nova" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "plan-old-one.md").write_text("# Old one\n", encoding="utf-8")
    (plans_dir / "plan-old-two.md").write_text("# Old two\n", encoding="utf-8")

    assert pa.backfill_from_project(proj) == 2
    assert {p.title for p in pa.list_archived_plans()} == {"Old one", "Old two"}


def test_backfill_is_idempotent(project):
    proj, archive = project
    pmt._persist_approved_plan("# Only one\n\nbody")
    pa.backfill_from_project(proj)
    pa.backfill_from_project(proj)
    assert len(list(archive.rglob("*.md"))) == 1


def test_title_falls_back_to_filename(project, tmp_path):
    """A plan with no heading still lists under something readable."""
    f = tmp_path / "plan-no-heading.md"
    f.write_text("just body text\nmore text\n", encoding="utf-8")
    assert pa._plan_title(f) == "plan-no-heading"


# ── where a plan is stored depends on who approved it ────────────────────────


def test_a_plan_nova_approved_itself_goes_to_the_global_folder_only(project):
    """Nobody reviewed it, so it must not land in the repo as if someone had."""
    proj, archive = project
    saved = pmt.persist_approved_plan("# Auto plan\n\nbody", auto_approved=True)

    assert not (proj / ".nova" / "plans").exists(), "nothing written into the project"
    copies = list(archive.rglob("*.md"))
    assert [c.name for c in copies] == ["plan-auto-plan.md"]
    assert copies[0].parent.name.startswith("myproject-")
    assert saved == str(copies[0])
    assert pa.list_archived_plans()[0].project == str(proj), "/plan list still finds it"


def test_a_user_approved_plan_goes_to_the_project_folder(project):
    proj, _ = project
    saved = pmt.persist_approved_plan("# User plan\n\nbody")
    assert (proj / ".nova" / "plans" / "plan-user-plan.md").exists()
    assert Path(saved) == Path(".nova/plans/plan-user-plan.md")


def test_an_empty_plan_saves_nothing(project):
    proj, archive = project
    assert pmt.persist_approved_plan("   ") is None
    assert not (proj / ".nova").exists() and not archive.exists()


class _State:
    def __init__(self, plan_agent=None):
        self.plan_agent = plan_agent


def test_a_plan_from_the_unresumed_planner_is_still_saved(project):
    """The regression: /plan's planner is never resumed after approval, so the
    save that sits after exit_plan_mode's interrupt() never ran — every /plan
    approval left nothing on disk."""
    from novacode_cli.core.agent_loop import _save_unresumed_plan

    proj, archive = project
    saved = _save_unresumed_plan(("# From the planner\n\nbody", False), _State())
    assert saved and (proj / ".nova" / "plans" / "plan-from-the-planner.md").exists()

    saved = _save_unresumed_plan(("# Self approved\n\nbody", True), _State())
    assert (archive / pa.project_slug(proj) / "plan-self-approved.md").exists()
    assert not (proj / ".nova" / "plans" / "plan-self-approved.md").exists()


def test_an_auto_approved_plan_the_planner_wrote_to_disk_reaches_the_global_folder(project):
    """No inline plan: the planner wrote its own file under .nova/plans/."""
    from novacode_cli.core.agent_loop import _save_unresumed_plan

    proj, archive = project
    plans = proj / ".nova" / "plans"
    plans.mkdir(parents=True)
    (plans / "draft.md").write_text("# Written by the planner\n\nbody", encoding="utf-8")

    _save_unresumed_plan(("", True), _State())
    assert (archive / pa.project_slug(proj) / "plan-written-by-the-planner.md").exists()

    assert _save_unresumed_plan(("", False), _State()) is None, "already in the project"
    assert _save_unresumed_plan(None, _State()) is None
