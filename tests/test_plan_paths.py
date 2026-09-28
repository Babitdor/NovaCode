"""Plan files are read from the same root exit_plan_mode writes them to.

exit_plan_mode saves approved plans under ``settings.get_workspace_root()``,
but the readers used two other roots — ``Path.cwd()`` (interrupt_handlers) and
``settings.project_root or Path.cwd()`` (plan_handler). They agree only while
Nova runs from the project root; launched from anywhere else, the plan was
written to the project and looked for somewhere else entirely, so approval
showed no plan and ``/plan`` found nothing.
"""

from __future__ import annotations

from pathlib import Path

import novacode_cli.tools.plan_mode_tools as pmt
from novacode_cli.config.config import settings
from novacode_cli.ui.interrupt_handlers import resolve_plan_content


class _SessionState:
    """No cached plan_content, so resolution falls through to the file."""


def _plans_root() -> Path:
    return settings.get_workspace_root() / ".nova" / "plans"


def test_writer_and_readers_agree_from_outside_the_project(
    monkeypatch, tmp_path
) -> None:
    """The bug: cwd outside the project root desynced writer and reader."""
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setattr(settings, "project_root", project, raising=False)
    # `monkeypatch.chdir`, not bare `os.chdir`: this test deliberately runs from
    # outside the project, and monkeypatch is what puts the cwd back. A leaked
    # cwd breaks every LATER test that reads a relative path — it took out
    # tests/test_research_swarm_roster.py, whose prompt path is relative, in any
    # run where this file came first.
    monkeypatch.chdir(elsewhere)

    saved = pmt._persist_approved_plan("# Refactor auth flow\n\nStep one.")
    assert saved, "plan should have been persisted"

    # Written under the project, not the cwd.
    written = project / ".nova" / "plans" / "plan-refactor-auth-flow.md"
    assert written.exists(), f"expected {written}"
    assert not (elsewhere / ".nova").exists(), "must not write beside the cwd"

    # And the reader finds it from the same root.
    content, plan_file = resolve_plan_content(None, _SessionState(), backend=None)
    assert content is not None, "reader missed the plan the writer just saved"
    assert "Refactor auth flow" in content
    assert plan_file == written


def test_inline_plan_short_circuits_the_file_lookup(monkeypatch, tmp_path) -> None:
    """An inline plan is authoritative — no filesystem read at all."""
    monkeypatch.setattr(settings, "project_root", tmp_path, raising=False)
    content, plan_file = resolve_plan_content(
        None, _SessionState(), backend=None, inline_plan="# Inline\n\nbody"
    )
    assert content == "# Inline\n\nbody"
    assert plan_file is None
