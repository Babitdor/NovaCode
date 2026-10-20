"""Host-scheduled DAG claims without model polling or workspace sharing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.models import EventCallback, GatewayCallback, Identity, SpawnRequest

if TYPE_CHECKING:
    from pathlib import Path


class ControlledHandle:
    def __init__(self, event: EventCallback) -> None:
        self.event = event
        self.prompts = []

    async def send(self, text: str):
        self.prompts.append(text)
        await self.event("running", {})

    async def stop(self):
        return True


class ControlledAdapter:
    def __init__(self) -> None:
        self.handles = []

    async def start(
        self, _request: SpawnRequest, event: EventCallback, _gateway: GatewayCallback
    ) -> object:
        handle = ControlledHandle(event)
        self.handles.append(handle)
        return handle


async def test_swarm_claims_only_ready_tasks_and_releases_dependencies(tmp_path: Path):
    adapter = ControlledAdapter()
    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"worktree": adapter}, swarm_enabled=True)
    team = await host.request(host.lead, "team_create", {"name": "swarm", "swarm": True})
    first = await host.request(
        host.lead,
        "task_create",
        {"team_id": team["id"], "title": "first", "description": "Inspect schema"},
    )
    second = await host.request(
        host.lead,
        "task_create",
        {
            "team_id": team["id"],
            "title": "second",
            "description": "Verify schema",
            "depends_on": [first["id"]],
        },
    )
    one = await host.spawn(host.lead, "Follow task constraints.", team["id"], mode="worktree")
    two = await host.spawn(host.lead, "Follow task constraints.", team["id"], mode="worktree")
    assert len(adapter.handles[0].prompts) == 1
    assert adapter.handles[1].prompts == []  # No empty startup LLM call.
    assert (await host.request(host.lead, "task_get", {"task_id": first["id"]}))["owner"] == one[
        "id"
    ]
    assert (await host.request(host.lead, "task_get", {"task_id": second["id"]}))[
        "status"
    ] == "pending"
    await adapter.handles[0].event("idle", {"summary": "Schema inspected", "verification": []})
    claimed = await host.request(host.lead, "task_get", {"task_id": second["id"]})
    assert claimed["status"] == "running"
    assert claimed["owner"] in {one["id"], two["id"]}
    assert sum(len(handle.prompts) for handle in adapter.handles) == 2
    await host.close()


async def test_manual_teams_do_not_autoclaim_pending_tasks(tmp_path: Path):
    adapter = ControlledAdapter()
    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"worktree": adapter}, swarm_enabled=True)
    team = await host.request(host.lead, "team_create", {"name": "manual"})
    task = await host.request(
        host.lead, "task_create", {"team_id": team["id"], "title": "read", "description": "read"}
    )
    await host.spawn(host.lead, "explicit handoff", team["id"], mode="worktree")
    assert adapter.handles[0].prompts == ["explicit handoff"]
    assert (await host.request(host.lead, "task_get", {"task_id": task["id"]}))[
        "status"
    ] == "pending"
    await host.close()
