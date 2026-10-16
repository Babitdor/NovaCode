"""Offline task-boundary and skill-proposal replays."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from langgraph.store.memory import InMemoryStore

from novacode_cli.commands.learning_handler import handle_learn_command
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.hermes.activation import ActivationManager, content_hash
from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.hermes.review import ReviewRunner
from novacode_cli.hermes.tracker import ToolUsageTracker

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), (False, False), ("off", False), (True, True)],
)
def test_learning_config_respects_unset_and_explicit_values(
    value: object,
    expected: object,
) -> None:
    config = object.__new__(NovaConfig)
    config._config = {} if value is None else {"learning_enabled": value}
    assert config.get_learning_enabled() is expected


@pytest.mark.asyncio
async def test_task_end_does_not_review_mid_task_or_reads() -> None:
    store = InMemoryStore()
    tracker = ToolUsageTracker(store)
    runner = ReviewRunner(store, tracker, None, mode="task_end")
    for tool in ("read_file", "grep", "execute"):
        await tracker.record_tool_usage(tool, success=tool != "execute")
    assert await runner.should_review() is False
    await tracker.reset_counter()
    await tracker.increment_counter()
    await tracker.record_tool_usage("read_file", success=True)
    assert await runner.should_consolidate() is False


@pytest.mark.asyncio
async def test_next_task_can_review_after_previous_review() -> None:
    store = InMemoryStore()
    tracker = ToolUsageTracker(store)
    runner = ReviewRunner(store, tracker, None, mode="task_end")
    tracker.start_task()
    await tracker.increment_counter()
    await tracker.record_tool_usage("execute", success=True)
    assert await runner.should_consolidate() is True
    completed = True
    await runner._set_review_just_completed(completed)
    tracker.start_task()
    await tracker.increment_counter()
    await tracker.record_tool_usage("execute", success=True)
    assert await runner.should_consolidate() is True


@pytest.mark.asyncio
async def test_learn_uses_agent_reading_and_staging_path() -> None:
    prompt = await handle_learn_command(SimpleNamespace(cmd_args="docs/example.md"))
    assert "normal reading tools" in prompt
    assert "skill_manage" in prompt
    assert "complete authored body" in prompt


def _skill(tmp_path: Path, name: str = "sample") -> Path:
    source = tmp_path / "skills" / name / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        f"---\nname: {name}\ndescription: Existing skill\n---\nOriginal step.\n",
        encoding="utf-8",
    )
    return source


def test_skill_edit_approval_activation_and_stale_hash(tmp_path: Path) -> None:
    manager = ActivationManager(tmp_path, "project")
    source = _skill(tmp_path)
    revised = "---\nname: sample\ndescription: Existing skill\n---\nRevised step.\n"
    proposal = manager.ledger.propose(
        content=revised,
        kind="skill",
        target="sample",
        project="project",
        source_hash=content_hash(source.read_bytes()),
        source_path=str(source),
    )
    assert manager.capture() == ()
    manager.ledger.transition(proposal.reference, "approve")
    assert manager.capture() == ()
    assert manager.apply_pending() == (("skill", "sample", revised),)
    assert "Original step" in source.read_text(encoding="utf-8")
    applied = manager.ledger.list()[0]
    manager.ledger.transition(applied.reference, "rollback")
    assert manager.apply_pending() == ()

    stale = manager.ledger.propose(
        content=revised,
        kind="skill",
        target="sample",
        project="project",
        source_hash=content_hash(source.read_bytes()),
        source_path=str(source),
    )
    source.write_text(source.read_text(encoding="utf-8") + "Manual change.\n", encoding="utf-8")
    manager.ledger.transition(stale.reference, "approve")
    assert manager.apply_pending() == ()
    assert manager.ledger.history(stale.id)[-1].status == "failed"


def test_archive_is_recoverable_and_only_applies_between_turns(tmp_path: Path) -> None:
    manager = ActivationManager(tmp_path, "project")
    source = _skill(tmp_path)
    proposal = manager.ledger.propose(
        content=source.read_text(encoding="utf-8"),
        kind="skill_archive",
        target="sample",
        project="project",
        source_path=str(source),
        source_hash=content_hash(source.read_bytes()),
    )
    assert source.is_file()
    manager.ledger.transition(proposal.reference, "approve")
    assert source.is_file()
    assert manager.apply_pending() == ()
    assert not source.exists()
    applied = manager.ledger.list()[0]
    manager.ledger.transition(applied.reference, "rollback")
    assert manager.apply_pending() == ()
    assert source.is_file()


def test_failed_archive_publication_restores_active_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.hermes import activation

    manager = ActivationManager(tmp_path, "project")
    source = _skill(tmp_path)
    proposal = manager.ledger.propose(
        content=source.read_text(encoding="utf-8"),
        kind="skill_archive",
        target="sample",
        project="project",
        source_path=str(source),
        source_hash=content_hash(source.read_bytes()),
    )
    manager.ledger.transition(proposal.reference, "approve")
    original_publish = activation.publish

    def fail_manifest(path: Path, content: bytes) -> None:
        if path == manager.path:
            message = "manifest unavailable"
            raise OSError(message)
        original_publish(path, content)

    monkeypatch.setattr(activation, "publish", fail_manifest)
    with pytest.raises(OSError, match="manifest unavailable"):
        manager.apply_pending()
    assert source.is_file()
    assert manager.ledger.list()[0].status == "approved"


@pytest.mark.asyncio
async def test_refine_stages_reviewed_memory_without_writing_active_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from novacode_cli.hermes import refine_loop

    agent_dir = tmp_path / "agent"
    monkeypatch.setattr(refine_loop, "_agent_dir", lambda: agent_dir)
    monkeypatch.setattr(refine_loop, "should_refine", AsyncMock(return_value=(True, "useful")))
    monkeypatch.setattr(refine_loop, "_gather_evidence", AsyncMock(return_value="check passed"))
    monkeypatch.setattr(refine_loop, "_review", AsyncMock(return_value=(True, "accepted")))
    monkeypatch.setattr(
        refine_loop,
        "plan_refinements",
        AsyncMock(
            return_value=[
                {
                    "domain": "memory",
                    "target": "testing",
                    "action": "update",
                    "reason": "observed check",
                    "change": "Run the focused test first",
                }
            ]
        ),
    )
    summary = await refine_loop.run_refine(InMemoryStore(), ToolUsageTracker(InMemoryStore()))
    assert summary["items"][0]["outcome"] == "proposed"
    assert summary["applied"] == 0
    assert not (agent_dir / "memories").exists()
    proposal = LearningLedger(agent_dir / "learning.sqlite3").list()[0]
    assert proposal.status == "candidate"
    assert proposal.content == "Run the focused test first"
