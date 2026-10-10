"""Learning has to be findable, and only what is general may be shared.

Each case here comes from inspecting what a real training run over twelve
unrelated Terminal-Bench tasks left behind, and from asking that memory what it
would hand an unseen task at different moments.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from novacode_cli.hermes import memory_tiers as mt
from novacode_cli.hermes.skill_manager import SkillManager
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware

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


# ── a lesson is recalled by its symptom ────────────────────────────────────


def _memory(tmp_path: Path, name: str, bullet: str) -> AgentMemoryMiddleware:
    mem = tmp_path / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / f"{name}.md").write_text(f"# {name}\n\n{bullet}\n", encoding="utf-8")
    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = tmp_path
    mw._corpus_cache = mw._corpus_sig = mw._retrieval_cache = None
    return mw


def _facing(request: str, tool_output: str) -> SimpleNamespace:
    return SimpleNamespace(
        messages=[
            {"role": "user", "content": request},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": tool_output},
        ]
    )


SYMPTOM_FIRST = (
    "- `bash: python3: command not found` in a bare container → `apt-get update && "
    "apt-get install -y python3`."
)
FIX_ONLY = (
    "- To get Python in a bare Ubuntu container run `apt-get update && apt-get install -y "
    "--no-install-recommends python3 python3-pip`."
)
ERROR = "bash: line 1: python3: command not found\nExit code: 127"
REQUEST = "Serve a static page with nginx on port 8080."


def test_a_short_error_recalls_the_lesson_that_quotes_it(tmp_path: Path) -> None:
    """An error line is short; it must not need three matching words like a listing does."""
    out = _memory(tmp_path, "bare-container-python", SYMPTOM_FIRST)._relevant_memories(
        _facing(REQUEST, ERROR)
    )
    assert "apt-get install -y python3" in out


def test_by_words_alone_a_fix_only_lesson_is_not_found(tmp_path: Path, monkeypatch) -> None:
    """Why the review must lead with the symptom: this is the lesson a run actually wrote.

    It shares no words with the error it is the fix for, so without the
    embedding model it cannot be recalled at all.
    """
    monkeypatch.setenv("NOVA_SEMANTIC_MEMORY", "0")
    out = _memory(tmp_path, "bare-container-python", FIX_ONLY)._relevant_memories(
        _facing(REQUEST, ERROR)
    )
    assert out == ""


def test_by_meaning_the_fix_only_lesson_is_found(tmp_path: Path) -> None:
    """The embedding connects the error to its fix even with no words in common."""
    import pytest

    from novacode_cli.memory import semantic

    if not semantic.enabled():
        pytest.skip("local embedding model not available")
    out = _memory(tmp_path, "bare-container-python", FIX_ONLY)._relevant_memories(
        _facing(REQUEST, ERROR)
    )
    assert "apt-get install -y" in out


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


def test_a_neighbouring_lesson_is_not_dragged_in_with_the_right_one(tmp_path: Path) -> None:
    """The apt error matches its own lesson (0.79) and, less, an unrelated pip note (0.62)."""
    import pytest

    from novacode_cli.memory import semantic

    if not semantic.enabled():
        pytest.skip("local embedding model not available")
    mem = tmp_path / "memories"
    mem.mkdir(parents=True)
    (mem / "tooling.md").write_text(
        "# Tooling\n\n"
        "- In a bare container `apt-get install` fails with `Unable to locate package` until "
        "`apt-get update` has run first.\n"
        "- `pip3 install` needs `--break-system-packages` under PEP 668 on Debian images.\n",
        encoding="utf-8",
    )
    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = tmp_path
    mw._corpus_cache = mw._corpus_sig = mw._retrieval_cache = None
    out = mw._relevant_memories(
        _facing(REQUEST, "E: Unable to locate package nginx\napt-get install failed, exit code: 100")
    )
    assert "apt-get update" in out and "PEP 668" not in out


def test_ordinary_output_still_needs_the_full_match(tmp_path: Path) -> None:
    harmless = "python3 --version\nPython 3.12.3\nthe command completed"
    out = _memory(tmp_path, "bare-container-python", SYMPTOM_FIRST)._relevant_memories(
        _facing(REQUEST, harmless)
    )
    assert out == ""


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


def test_one_task_renaming_a_skill_does_not_fill_the_ledger(tmp_path: Path) -> None:
    """One task proposed its "raw tensor dump" skill under five names."""
    for name in (
        "inspect-raw-tensor-dump",
        "reverse-engineer-raw-tensor-dump",
        "inspect-raw-tensor-checkpoint",
    ):
        assert _propose(tmp_path, "sandbox-task-a", name) == []
    ledger = json.loads((tmp_path / "skills" / ".skill-proposals.json").read_text(encoding="utf-8"))
    assert len(ledger) == 1, list(ledger)
    # A second task wanting it is still what creates the skill.
    assert _propose(tmp_path, "sandbox-task-b", "inspect-raw-tensor-dump") == [
        "inspect-raw-tensor-dump"
    ]
