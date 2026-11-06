"""Real Git isolation and publication authorization regressions."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from novacode_cli.prime_factory.publication import Publisher
from novacode_cli.prime_factory.workspace import FactoryWorkspace, WorkspaceError
from novacode_cli.sessions import worktree


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FactoryWorkspace:
    source = tmp_path / "project"
    source.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "offline@example.invalid"],
        ["config", "user.name", "Offline"],
    ):
        assert worktree._git(args, cwd=source)[0] == 0
    (source / "one.py").write_text("value = 1\n")
    (source / "two.py").write_text("value = 2\n")
    assert worktree._git(["add", "."], cwd=source)[0] == 0
    assert worktree._git(["commit", "-m", "baseline"], cwd=source)[0] == 0
    monkeypatch.setattr(worktree, "worktrees_root", lambda: tmp_path / "workers")
    return FactoryWorkspace(source)


def commit_task(record: dict, name: str, content: str) -> str:
    root = Path(record["path"])
    (root / name).write_text(content)
    assert worktree._git(["add", "--", name], cwd=root)[0] == 0
    assert worktree._git(["commit", "-m", "task"], cwd=root)[0] == 0
    return worktree._git(["rev-parse", "HEAD"], cwd=root)[1]


def test_dirty_checkout_requires_explicit_baseline_and_preserves_edits(factory: FactoryWorkspace):
    (factory.source / "one.py").write_text("user edit\n")
    with pytest.raises(WorkspaceError, match="Dirty checkout"):
        factory.prepare("dirty")
    record = factory.prepare("dirty", baseline="committed")
    assert (Path(record["path"]) / "one.py").read_text() == "value = 1\n"
    assert (factory.source / "one.py").read_text() == "user edit\n"


def test_independent_tasks_integrate_and_ownership_is_leased(factory: FactoryWorkspace):
    factory.prepare("parallel")
    one = factory.task("one", files=["one.py"])
    two = factory.task("two", files=["two.py"])
    with pytest.raises(WorkspaceError, match="overlap"):
        factory.task("overlap", files=["ONE.py"])
    with pytest.raises(WorkspaceError, match="dependencies"):
        factory.task("premature", files=["third.py"], depends_on=("one",))
    first_revision = commit_task(one, "one.py", "value = 10\n")
    second_revision = commit_task(two, "two.py", "value = 20\n")
    factory.integrate("one", first_revision)
    factory.integrate("two", second_revision)
    candidate = factory.candidate()
    assert "value = 10" in candidate["diff"]
    assert "value = 20" in candidate["diff"]
    assert (factory.source / "one.py").read_text() == "value = 1\n"
    dependent = factory.task("after", files=["one.py"], depends_on=("one", "two"))
    assert (Path(dependent["path"]) / "two.py").read_text() == "value = 20\n"


def test_outside_ownership_and_dirty_candidate_block(factory: FactoryWorkspace):
    factory.prepare("boundaries")
    with pytest.raises(WorkspaceError, match="inside"):
        factory.task("escape", files=["../escape"])
    task = factory.task("one", files=["one.py"])
    revision = commit_task(task, "two.py", "outside ownership\n")
    with pytest.raises(WorkspaceError, match="outside"):
        factory.integrate("one", revision)
    root = Path(factory.snapshot()["path"])
    (root / "one.py").write_text("dirty\n")
    with pytest.raises(WorkspaceError, match="committed and clean"):
        factory.candidate()


def test_conflict_preserves_both_worktrees(factory: FactoryWorkspace):
    factory.prepare("conflict")
    task = factory.task("one", files=["one.py"])
    revision = commit_task(task, "one.py", "task change\n")
    integration = {"path": factory.snapshot()["path"]}
    commit_task(integration, "one.py", "integration change\n")
    with pytest.raises(WorkspaceError):
        factory.integrate("one", revision)
    assert factory.tasks["one"]["status"] == "conflict"
    assert Path(task["path"]).is_dir()
    assert (factory.source / "one.py").read_text() == "value = 1\n"
    assert "<<<<<<<" in (Path(integration["path"]) / "one.py").read_text()


def test_restore_revalidates_workspace(factory: FactoryWorkspace):
    factory.prepare("restore")
    task = factory.task("active", files=["one.py"])
    saved = factory.snapshot()
    restored = FactoryWorkspace(factory.source)
    restored.restore(saved)
    with pytest.raises(WorkspaceError, match="overlap"):
        restored.task("other", files=["one.py"])
    assert Path(task["path"]).exists()


def test_intermediate_unowned_edits_cannot_be_hidden_by_reverting(factory: FactoryWorkspace):
    factory.prepare("revert")
    task = factory.task("one", files=["one.py"])
    commit_task(task, "two.py", "unowned change\n")
    commit_task(task, "two.py", "value = 2\n")
    revision = commit_task(task, "one.py", "approved change\n")
    with pytest.raises(WorkspaceError, match="commit changed files outside"):
        factory.integrate("one", revision)


def test_environment_changes_invalidate_candidate_identity(factory: FactoryWorkspace):
    factory.prepare("environment")
    one = factory.candidate(environment={"python": "3.11"})
    two = factory.candidate(environment={"python": "3.12"})
    assert one["revision"] == two["revision"]
    assert one["id"] != two["id"]


async def test_publication_requires_current_exact_approval_and_durable_intent(
    factory: FactoryWorkspace,
):
    await asyncio.to_thread(factory.prepare, "publish")
    candidate = await asyncio.to_thread(factory.candidate)
    saved, commands = [], []

    async def persist(record: dict) -> None:
        saved.append(record)

    async def execute(argv: list[str], cwd: Path) -> tuple[int, str, str]:
        assert cwd == Path(candidate["workspace"])
        assert saved
        commands.append(argv)
        return 0, "[]" if argv[:3] == ["gh", "pr", "list"] else "https://github.com/o/r/pull/1", ""

    publisher = Publisher(Path(candidate["workspace"]), persist, execute)
    intent = publisher.prepare(
        candidate, destination="o/r", branch="factory/run", title="Goal", body="Proof"
    )
    with pytest.raises(WorkspaceError, match="exact intent"):
        await publisher.publish(intent, approved_intent=candidate["id"])
    assert not commands
    result = await publisher.publish(intent, approved_intent=intent["id"])
    assert result["status"] == "published"
    assert len(commands) == 3
    assert [record["status"] for record in saved] == ["publishing", "pushed", "published"]
    altered = {**intent, "branch": "another"}
    with pytest.raises(WorkspaceError, match="exact intent"):
        await publisher.publish(altered, approved_intent=intent["id"])
    await asyncio.to_thread(commit_task, {"path": candidate["workspace"]}, "one.py", "changed\n")
    with pytest.raises(WorkspaceError, match="expired"):
        await publisher.publish(intent, approved_intent=intent["id"])


async def test_publication_reconciles_uncertain_creation_without_duplicate(
    factory: FactoryWorkspace,
):
    await asyncio.to_thread(factory.prepare, "uncertain")
    candidate = await asyncio.to_thread(factory.candidate)
    commands = []

    async def persist(record: dict) -> None:
        pass

    async def execute(argv: list[str], cwd: Path) -> tuple[int, str, str]:
        commands.append((argv, cwd))
        return (
            0,
            json.dumps(
                [
                    {
                        "url": "pr/1",
                        "headRefOid": candidate["revision"],
                        "baseRefName": "main",
                        "title": "Goal",
                        "body": "Proof",
                        "state": "OPEN",
                    }
                ]
            ),
            "",
        )

    publisher = Publisher(Path(candidate["workspace"]), persist, execute)
    intent = publisher.prepare(
        candidate, destination="o/r", branch="factory/run", title="Goal", body="Proof"
    )
    result = await publisher.publish(intent, approved_intent=intent["id"])
    assert result["url"] == "pr/1"
    assert len(commands) == 1


async def test_storage_failure_prevents_publication(factory: FactoryWorkspace):
    await asyncio.to_thread(factory.prepare, "storage")
    candidate = await asyncio.to_thread(factory.candidate)

    async def persist(_record: dict) -> None:
        msg = "disk full"
        raise OSError(msg)

    async def execute(argv: list[str], cwd: Path) -> tuple[int, str, str]:
        pytest.fail(f"Unexpected side effect {argv} {cwd}")

    publisher = Publisher(Path(candidate["workspace"]), persist, execute)
    intent = publisher.prepare(
        candidate, destination="o/r", branch="factory/run", title="Goal", body="Proof"
    )
    with pytest.raises(OSError, match="disk full"):
        await publisher.publish(intent, approved_intent=intent["id"])


def test_integration_reconciles_crash_after_cherry_pick(factory: FactoryWorkspace):
    factory.prepare("integration-restart")
    task = factory.task("one", files=["one.py"])
    revision = commit_task(task, "one.py", "value = 42\n")
    saved = deepcopy(factory.snapshot())
    integrated = factory.integrate("one", revision)
    restarted = FactoryWorkspace(factory.source)
    restarted.restore(saved)
    assert restarted.integrate("one", revision) == integrated
    assert restarted.tasks["one"]["status"] == "integrated"
    assert worktree._git(["status", "--porcelain"], cwd=Path(saved["path"]))[1] == ""


async def test_unavailable_push_is_never_certified_as_publication(factory: FactoryWorkspace):
    factory.prepare("unavailable-push")
    candidate = factory.candidate()
    calls, saved = [], []

    async def persist(record: dict) -> None:
        saved.append(record)

    async def execute(argv: list[str], _cwd: Path) -> tuple[int | None, str, str]:
        calls.append(argv)
        return (0, "[]", "") if argv[:3] == ["gh", "pr", "list"] else (None, "", "unavailable")

    publisher = Publisher(Path(candidate["workspace"]), persist, execute)
    intent = publisher.prepare(
        candidate, destination="o/r", branch="factory/run", title="Goal", body="Proof"
    )
    with pytest.raises(WorkspaceError, match="unavailable"):
        await publisher.publish(intent, approved_intent=intent["id"])
    assert len(calls) == 2
    assert saved[-1]["status"] == "publishing"


async def test_reconciliation_rejects_wrong_pr_base(factory: FactoryWorkspace):
    factory.prepare("wrong-base")
    candidate = factory.candidate()

    async def persist(_record: dict) -> None:
        pass

    async def execute(_argv: list[str], _cwd: Path) -> tuple[int, str, str]:
        return (
            0,
            json.dumps(
                [
                    {
                        "url": "pr/1",
                        "headRefOid": candidate["revision"],
                        "baseRefName": "other",
                        "title": "Goal",
                        "body": "Proof",
                        "state": "OPEN",
                    }
                ]
            ),
            "",
        )

    publisher = Publisher(Path(candidate["workspace"]), persist, execute)
    intent = publisher.prepare(
        candidate, destination="o/r", branch="factory/run", title="Goal", body="Proof"
    )
    with pytest.raises(WorkspaceError, match="different or ambiguous"):
        await publisher.publish(intent, approved_intent=intent["id"])
