"""Recent plan lookup crosses storage scopes without crossing project boundaries."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from novacode_cli.plan_archive import global_plan_dir
from novacode_cli.tools.plan_archive_tools import recent_plans
from novacode_cli.tools.plan_mode_tools import persist_approved_plan

if TYPE_CHECKING:
    from pathlib import Path


def test_empty_lookup_does_not_create_folders(isolated_plan_storage: tuple[Path, Path]) -> None:
    project, archive = isolated_plan_storage
    result = json.loads(recent_plans.invoke({}))
    assert result["plans"] == []
    assert not (project / ".nova").exists()
    assert not archive.exists()


def test_recent_plans_include_both_scopes_and_full_markdown(
    isolated_plan_storage: tuple[Path, Path],
) -> None:
    project, _ = isolated_plan_storage
    manual = "# User approved\n\nExact constraints: keep every line.\n"
    automatic = "# Automatically approved\n\n## Verification\nRun the parser tests.\n"
    persist_approved_plan(manual)
    persist_approved_plan(automatic, auto_approved=True)
    auto_file = global_plan_dir(project) / "plan-automatically-approved.md"
    os.utime(auto_file, (2_000_000_000, 2_000_000_000))
    result = json.loads(recent_plans.invoke({}))
    assert [entry["path"] for entry in result["plans"]] == [
        "global/plan-automatically-approved.md",
        "project/plan-user-approved.md",
    ]
    for entry, expected in zip(result["plans"], [automatic, manual], strict=True):
        assert recent_plans.invoke({"action": "read", "path": entry["path"]}) == expected
    assert len(json.loads(recent_plans.invoke({"limit": 1}))["plans"]) == 1


def test_other_projects_are_excluded(isolated_plan_storage: tuple[Path, Path]) -> None:
    project, _ = isolated_plan_storage
    other = global_plan_dir(project.parent / "other-project") / "plan-other.md"
    other.write_text("# Another project", encoding="utf-8")
    assert json.loads(recent_plans.invoke({}))["plans"] == []


def test_different_archive_revision_is_preserved(isolated_plan_storage: tuple[Path, Path]) -> None:
    project, _ = isolated_plan_storage
    persist_approved_plan("# Revision\nOriginal")
    archived = global_plan_dir(project) / "plan-revision.md"
    archived.write_text("# Revision\nDifferent constraints", encoding="utf-8")
    assert len(json.loads(recent_plans.invoke({}))["plans"]) == 2


@pytest.mark.usefixtures("isolated_plan_storage")
@pytest.mark.parametrize("path", ["project/../../secret.md", "global/../secret.md", "C:/secret.md"])
def test_read_rejects_paths_outside_plan_folder(
    path: str,
) -> None:
    assert recent_plans.invoke({"action": "read", "path": path}).startswith("Error:")


@pytest.mark.usefixtures("isolated_plan_storage")
def test_missing_plan_returns_an_error() -> None:
    assert recent_plans.invoke({"action": "read", "path": "global/missing.md"}).startswith("Error")
