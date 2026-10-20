"""Runtime contracts, exercised with scripted transport boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.teams.context import conversation_snapshot
from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.models import Identity, TeamError
from novacode_cli.teams.scheduler import ProcessBudget

if TYPE_CHECKING:
    from pathlib import Path


def test_snapshot_is_independent_paired_and_preserves_corrections():
    messages = [
        HumanMessage("Use SQLite. Correction: never PostgreSQL."),
        AIMessage("", tool_calls=[{"name": "read_file", "args": {}, "id": "a"}]),
        ToolMessage("source", tool_call_id="a"),
        AIMessage("", tool_calls=[{"name": "spawn_agent", "args": {}, "id": "pending"}]),
    ]
    snapshot, digest = conversation_snapshot(messages, 1000)
    assert len(snapshot) == 3
    assert "never PostgreSQL" in snapshot[0].content
    snapshot[0].content = "changed"
    assert messages[0].content != "changed"
    assert len(digest) == 64
    with pytest.raises(TeamError, match="overflow"):
        conversation_snapshot(messages, 1)


def test_reservations_are_bounded_and_released_once():
    budget = ProcessBudget(1)
    budget.reserve("a")
    with pytest.raises(TeamError):
        budget.reserve("b")
    budget.release("a")
    budget.release("a")
    budget.reserve("b")
    assert budget.used == 1


async def test_cancel_is_scoped_and_team_deletion_stops_owned_workers(tmp_path: Path):
    adapter = AsyncMock()
    handles = []

    async def start(*_args: object, **_kwargs: object) -> object:
        handle = AsyncMock()
        handle.stop.return_value = True
        handles.append(handle)
        return handle

    adapter.start.side_effect = start
    coordinator = TeamCoordinator(tmp_path, Identity("p", "r"), {"default": adapter})
    team = await coordinator.request(coordinator.lead, "team_create", {"name": "test"})
    one = await coordinator.spawn(coordinator.lead, "first", team["id"])
    two = await coordinator.spawn(coordinator.lead, "second", team["id"])
    await coordinator.cancel(coordinator.lead, one["id"])
    handles[0].stop.assert_awaited_once()
    handles[1].stop.assert_not_awaited()
    await coordinator.delete_team(coordinator.lead, team["id"])
    handles[1].stop.assert_awaited_once()
    assert (await coordinator.request(coordinator.lead, "get_agent", {"agent_id": two["id"]}))[
        "status"
    ] == "stopped"
    await coordinator.close()


async def test_swarm_disabled_and_shared_workspace_modes_are_rejected(tmp_path: Path):
    coordinator = TeamCoordinator(tmp_path, Identity("p", "r"), {})
    with pytest.raises(TeamError, match="swarm"):
        await coordinator.request(coordinator.lead, "team_create", {"name": "swarm", "swarm": True})
    coordinator.swarm_enabled = True
    team = await coordinator.request(
        coordinator.lead, "team_create", {"name": "swarm", "swarm": True}
    )
    with pytest.raises(TeamError, match="isolat"):
        await coordinator.spawn(coordinator.lead, "task", team["id"], mode="default")
    await coordinator.close()


async def test_uncertain_stop_retains_reservation_and_blocks_archival(tmp_path: Path):
    handle = AsyncMock()
    handle.stop.return_value = False
    adapter = AsyncMock()
    adapter.start.return_value = handle
    coordinator = TeamCoordinator(tmp_path, Identity("p", "r"), {"remote": adapter})
    team = await coordinator.request(coordinator.lead, "team_create", {"name": "test"})
    worker = await coordinator.spawn(coordinator.lead, "task", team["id"], mode="remote")
    with pytest.raises(TeamError, match="uncertain"):
        await coordinator.delete_team(coordinator.lead, team["id"])
    record = await coordinator.request(coordinator.lead, "get_agent", {"agent_id": worker["id"]})
    assert record["status"] == "disconnected"
    handle.stop.return_value = True
    await coordinator.close()


async def test_two_roots_do_not_share_agent_records(tmp_path: Path):
    first = TeamCoordinator(tmp_path, Identity("p", "one"), {})
    second = TeamCoordinator(tmp_path, Identity("p", "two"), {})
    team = await first.request(first.lead, "team_create", {"name": "private"})
    with pytest.raises(TeamError):
        await second.request(second.lead, "task_list", {"team_id": team["id"]})
    await asyncio.gather(first.close(), second.close())


async def test_two_coordinators_cannot_own_the_same_root(tmp_path: Path):
    first = TeamCoordinator(tmp_path, Identity("p", "shared-root"), {})
    second = TeamCoordinator(tmp_path, Identity("p", "shared-root"), {})
    await first.request(first.lead, "team_create", {"name": "owned"})
    try:
        with pytest.raises(TeamError, match="owner"):
            await second.request(second.lead, "team_create", {"name": "intruder"})
    finally:
        await first.close()
        await second.close()
