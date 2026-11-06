"""Host approval, shared budgets, persistence and cleanup invariants."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.prime_factory import ROLES
from novacode_cli.prime_factory.controller import FactoryController
from novacode_cli.prime_factory.state import digest, plan_definition, validate_checks
from novacode_cli.prime_factory.store import FactoryError, FactoryStore

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def controller(tmp_path: Path) -> Iterator[FactoryController]:
    store = FactoryStore(tmp_path / "factory.sqlite3")
    c = FactoryController.create(
        tmp_path,
        "Fix a behavior",
        "thread",
        {role: {"provider": "offline", "model": "scripted"} for role in ROLES},
        store=store,
        session=SimpleNamespace(),
    )
    yield c
    store.close()


@pytest.mark.parametrize("action", ["request_pause", "request_cancel"])
async def test_late_approval_cannot_revive_a_suspended_run(
    controller: FactoryController, action: str
):
    task = asyncio.create_task(
        controller.wait_approval("plan", {}, digest(plan_definition(controller.record)))
    )
    event = await controller.events.get()
    getattr(controller, action)()
    if not event.future.done():
        event.future.set_result({"response": {"approved": True}})
    assert await task is False
    assert controller.record["status"] == ("cancelled" if action == "request_cancel" else "paused")
    assert "plan" not in controller.record["approvals"]


async def test_pause_finishes_pending_approval_without_an_answer(controller: FactoryController):
    task = asyncio.create_task(
        controller.wait_approval("plan", {}, digest(plan_definition(controller.record)))
    )
    await controller.events.get()
    controller.request_cancel()
    assert await asyncio.wait_for(task, timeout=1) is False
    controller.request_pause()
    assert controller.record["status"] == "cancelled"
    with pytest.raises(FactoryError, match="expired approval"):
        controller.approve("plan", digest(plan_definition(controller.record)))
    with pytest.raises(FactoryError, match="cannot be resumed"):
        await controller.resume()


def test_shared_clock_excludes_only_all_worker_human_waits(
    controller: FactoryController, monkeypatch: pytest.MonkeyPatch
):
    clock = [0.0]
    monkeypatch.setattr("novacode_cli.prime_factory.controller.time.monotonic", lambda: clock[0])
    controller._clock = 0
    controller.record["usage"]["active_seconds"] = 0
    first = controller.start_attempt("requirements", "first", Path(controller.record["root"]))
    second = controller.start_attempt("research", "second", Path(controller.record["root"]))
    clock[0] = 10
    controller.begin_wait(first["id"])
    clock[0] = 20
    controller.tick()
    assert controller.record["usage"]["active_seconds"] == 20
    controller.begin_wait(second["id"])
    clock[0] = 200
    controller.tick()
    assert controller.record["usage"]["active_seconds"] == 20
    controller.end_wait(first["id"])
    clock[0] = 205
    controller.tick()
    assert controller.record["usage"]["active_seconds"] == 25


async def test_plan_approval_wait_does_not_spend_active_time(
    controller: FactoryController, monkeypatch: pytest.MonkeyPatch
):
    clock = [0.0]
    monkeypatch.setattr("novacode_cli.prime_factory.controller.time.monotonic", lambda: clock[0])
    controller._clock = 0
    controller.record["usage"]["active_seconds"] = 0
    task = asyncio.create_task(
        controller.wait_approval("plan", {}, digest(plan_definition(controller.record)))
    )
    event = await controller.events.get()
    clock[0] = 300
    event.future.set_result({"response": {"approved": True}})
    assert await task
    assert controller.record["usage"]["active_seconds"] == 0
    clock[0] = 305
    controller.tick()
    assert controller.record["usage"]["active_seconds"] == 5


def test_session_and_progress_omit_bulk_artifacts_but_durable_record_retains_them(
    controller: FactoryController,
):
    record = controller.record
    record["candidate"] = {
        "id": "candidate",
        "revision": "sha",
        "diff": "FULL PATCH",
        "workspace": "workspace",
    }
    record["attempts"] = [
        {"id": "attempt", "status": "complete", "result": {"raw": "MODEL OUTPUT"}}
    ]
    record["checks"] = [
        {
            "id": "check",
            "status": "passed",
            "evidence": [{"id": "evidence", "output": "COMMAND OUTPUT"}],
        }
    ]
    record["sources"] = [
        {
            "id": "source",
            "content": {
                "url": "https://example.org",
                "excerpt": "LONG EXCERPT",
                "decisions": ["DECISION"],
            },
        }
    ]
    record["publication"] = {
        "id": "intent",
        "candidate": record["candidate"],
        "body": "PR BODY",
        "status": "prepared",
    }
    controller.save()
    assert controller.session.prime_report == controller.progress()
    snapshot = json.dumps(controller.progress())
    assert all(
        value not in snapshot
        for value in ["FULL PATCH", "MODEL OUTPUT", "COMMAND OUTPUT", "LONG EXCERPT", "PR BODY"]
    )
    restored = controller.store.load(record["id"])
    assert restored["candidate"]["diff"] == "FULL PATCH"
    assert restored["checks"][0]["evidence"][0]["output"] == "COMMAND OUTPUT"
    controller.session.prime_report["candidate"]["id"] = "modified"
    assert record["candidate"]["id"] == "candidate"


async def test_storage_failure_does_not_prevent_stopping_every_owned_worker(
    controller: FactoryController, monkeypatch: pytest.MonkeyPatch
):
    controller.record["attempts"] = [
        {"id": "first", "status": "running"},
        {"id": "second", "status": "running"},
    ]
    first = SimpleNamespace(stop=AsyncMock(side_effect=OSError("uncertain")))
    second = SimpleNamespace(stop=AsyncMock(return_value=True))
    controller.handles = {"first": first, "second": second}
    controller._storage_failed = True
    save = Mock(side_effect=AssertionError("Saving unavailable storage is forbidden"))
    monkeypatch.setattr(controller, "save", save)
    controller.request_pause()
    await controller.stop_workers()
    assert first.stop.await_count == second.stop.await_count == 1
    assert "first" in controller.handles
    assert "second" not in controller.handles
    assert controller.record["attempts"][0]["status"] == "uncertain"
    save.assert_not_called()


def test_failed_persistence_fences_every_new_dispatch(
    controller: FactoryController, monkeypatch: pytest.MonkeyPatch
):
    save = Mock(side_effect=OSError("disk unavailable"))
    monkeypatch.setattr(controller.store, "save", save)
    with pytest.raises(FactoryError, match="storage failed"):
        controller.save()
    assert controller._storage_failed
    assert controller.record["status"] == "paused"
    with pytest.raises(FactoryError, match="suspended"):
        controller.start_attempt("research", "task", Path(controller.record["root"]))
    assert controller.record["attempts"] == []
    assert save.call_count == 1


def test_concurrent_saves_of_one_live_record_do_not_falsely_steal_ownership(
    controller: FactoryController,
):
    before = controller.record["store_revision"]
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: controller.store.save(controller.record), range(30)))
    assert controller.record["store_revision"] == before + 30
    assert controller.store.load(controller.record["id"])["store_revision"] == before + 30
    stale = deepcopy(controller.record)
    controller.store.save(controller.record)
    with pytest.raises(FactoryError, match="ownership changed"):
        controller.store.save(stale)


def test_acceptance_definition_change_invalidates_approval(controller: FactoryController):
    controller.record["criteria"] = ["Expected behavior"]
    controller.record["checks"] = validate_checks(
        [{"argv": ["python", "check.py"], "criteria": [0], "purpose": "Behavior"}],
        ["Expected behavior"],
    )
    controller.approve("plan", digest(plan_definition(controller.record)))
    controller.record["checks"][0]["argv"] = ["echo", "pass"]
    with pytest.raises(FactoryError, match="human approval"):
        controller.require_plan()


def test_acceptance_ownership_is_validated_and_frozen(controller: FactoryController):
    raw = {"argv": ["python", "check.py"], "criteria": [0], "purpose": "Behavior"}
    check = validate_checks([raw], ["Behavior"], {"owner"})[0]
    assert check["tasks"] == ["owner"]
    with pytest.raises(FactoryError, match="responsible planned tasks"):
        validate_checks([{**raw, "tasks": ["foreign"]}], ["Behavior"], {"owner"})
    controller.record["criteria"] = ["Behavior"]
    controller.record["checks"] = [check]
    controller.approve("plan", digest(plan_definition(controller.record)))
    check["tasks"] = ["another"]
    with pytest.raises(FactoryError, match="human approval"):
        controller.require_plan()


def test_declared_failure_and_success_literals_are_mechanical_assertions(
    controller: FactoryController,
):
    raw = {
        "argv": ["python", "check.py"],
        "criteria": [0],
        "purpose": "Behavior",
        "test_first": True,
        "failure_contains": ["assertion failed: expected 42"],
        "contains": ["behavior passed"],
    }
    check = validate_checks([raw], ["Behavior"])[0]
    controller.record["criteria"] = ["Behavior"]
    controller.record["checks"] = [check]
    controller.approve("plan", digest(plan_definition(controller.record)))
    controller.check_result(
        check, "before", "env", raw["argv"], 1, "ImportError: dependency missing", baseline=True
    )
    assert not check["baseline_failed"]
    controller.check_result(
        check, "before", "env", raw["argv"], 1, "assertion failed: expected 42", baseline=True
    )
    assert check["baseline_failed"]
    controller.check_result(check, "after", "env", raw["argv"], 0, "unrelated output")
    assert check["status"] == "failed"
    controller.check_result(check, "after", "env", raw["argv"], 0, "behavior passed")
    assert check["status"] == "passed"
    check["failure_contains"] = []
    with pytest.raises(FactoryError, match="human approval"):
        controller.require_plan()


async def test_unknown_reservations_are_charged_to_their_role_on_resume(
    controller: FactoryController,
):
    attempt = controller.start_attempt("research", "research", Path(controller.record["root"]))
    await controller.reserve(attempt["id"], "call", 100)
    attempt["status"] = "stopped"
    controller.request_pause()
    await controller.resume()
    assert controller.record["usage"]["tokens"] == 100
    assert controller.record["usage"]["by_role"]["research"] == {
        "tokens": 100,
        "estimated_calls": 1,
    }
    assert controller.record["reservations"] == {}


def test_saved_listings_do_not_load_execution_or_diff_artifacts(controller: FactoryController):
    controller.record["candidate"] = {"id": "candidate", "diff": "large private artifact" * 10000}
    controller.record["attempts"] = [
        {
            "id": "worker",
            "status": "complete",
            "role": "developer",
            "result": {"summary": "large output" * 10000},
        }
    ]
    controller.save()
    rows = controller.store.records()
    assert rows[0]["summary_only"]
    assert "candidate" not in rows[0]
    assert "attempts" not in rows[0]
    assert len(json.dumps(rows)) < 10000
    assert (
        "large private artifact"
        in controller.store.load(controller.record["id"])["candidate"]["diff"]
    )


def test_credentials_never_enter_durable_factory_records(
    controller: FactoryController, monkeypatch: pytest.MonkeyPatch
):
    secret = "private-factory-key-123456789"  # noqa: S105 - Synthetic exclusion fixture.
    monkeypatch.setenv("FACTORY_TEST_API_KEY", secret)
    controller.record["goal"] = "Do not persist " + secret
    with pytest.raises(FactoryError, match="storage failed"):
        controller.save()
    assert controller._storage_failed
    assert secret not in json.dumps(controller.store.load(controller.record["id"]))
