"""Learning stays explicitly searchable, and only what is general may be shared.

These cases cover lesson quality and scope. Lesson bodies are now retrieved
through explicit memory search rather than injected into unrelated turns.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from novacode_cli.hermes import memory_tiers as mt
from novacode_cli.hermes.skill_manager import SkillManager

APT = "- `apt-get install` needs `apt-get update` first in a fresh container"


# ── confirmation needs agreement on the fact, not its boilerplate ──────────


def test_shared_boilerplate_is_not_confirmation(tmp_path: Path) -> None:
    """A MIPS toolchain note was shared because it too mentioned `apt-get update`."""
    for project in ("task-a", "task-b"):
        mt.update_from_review(
            tmp_path, "", [{"topic": "apt", "bullets": APT, "scope": "general"}], project=project
        )
    mips = (
        "- `gcc-mipsel-linux-gnu` installs the `mipsel-linux-gnu-gcc` cross-compiler on "
        "Ubuntu 24.04; `apt-get update` must run first or the package is not found"
    )
    mt.update_from_review(
        tmp_path,
        "",
        [{"topic": "mips-cross-toolchain", "bullets": mips, "scope": "general"}],
        project="task-c",
    )
    shared = sorted(p.stem for p in mt.lesson_dir(tmp_path).glob("*.md") if p.name != "INDEX.md")
    assert shared == ["apt"], "one project's toolchain detail reached the shared pool"


def test_look_alike_bullets_with_a_different_value_are_both_kept(tmp_path: Path) -> None:
    """`pytest -x` vs `pytest -q` scores 0.999 by embedding; they are different facts."""
    for bullet in (
        "- Tests are run with `pytest -x` for fast feedback",
        "- Tests are run with `pytest -q` for fast feedback",
        "- The server listens on port 8080",
        "- The server listens on port 9090",
    ):
        mt.record_lesson(tmp_path, "project-facts", bullet)
    body = (mt.lesson_dir(tmp_path) / "project-facts.md").read_text(encoding="utf-8")
    assert len(mt._bullets(body)) == 4, body


def test_the_review_asks_for_the_symptom_before_the_fix() -> None:
    from novacode_cli.prompts import render_template

    text = render_template(
        "nova_review.jinja",
        tool_call_count=10,
        prior_lessons="",
        recovered_from_error=False,
        clean_win=False,
        existing_topics="",
    )
    assert "lead with the symptom" in text and "quote the error text" in text


# ── one held skill proposal per idea per project ───────────────────────────

_SKILL = (
    "<skill><name>{name}</name><scope>general</scope>"
    "<description>Use when doing the thing.</description>"
    "<body>## When to Use\nx\n## Procedure\n1. do it\n</body></skill>"
)


def _propose(tmp_path: Path, project: str, name: str) -> list[str]:
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    manager = SkillManager(None, skills_dir=skills, enabled=True, project=project)  # type: ignore[arg-type]
    manager._log_refinement = lambda *a, **k: None
    asyncio.run(manager.maybe_create_from_review(_SKILL.format(name=name)))
    return sorted(p.name for p in skills.iterdir() if p.is_dir())


def test_one_task_renaming_a_skill_does_not_fill_the_ledger(tmp_path: Path, monkeypatch) -> None:
    """One task proposed its "raw tensor dump" skill under five names."""
    from novacode_cli.hermes.ledger import LearningLedger

    monkeypatch.setattr(
        "novacode_cli.hermes.skill_proposals.main_agent_learning_ledger",
        lambda: LearningLedger(tmp_path / "learning.sqlite3"),
    )
    for name in (
        "inspect-raw-tensor-dump",
        "reverse-engineer-raw-tensor-dump",
        "inspect-raw-tensor-checkpoint",
    ):
        assert _propose(tmp_path, "sandbox-task-a", name) == []
    ledger = json.loads((tmp_path / "skills" / ".skill-proposals.json").read_text(encoding="utf-8"))
    assert len(ledger) == 1, list(ledger)
    # A second task wanting it is still what creates the skill.
    assert _propose(tmp_path, "sandbox-task-b", "inspect-raw-tensor-dump") == []
    records = LearningLedger(tmp_path / "learning.sqlite3").list()
    assert len(records) == 1
    assert records[0].target == "inspect-raw-tensor-dump"
    assert records[0].status == "candidate"
