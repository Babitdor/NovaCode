"""Multiple retained teammates share only the profile worker and owner transport."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

import aiohttp
import pytest

from novacode_cli.teammates.controller import TeammateController
from novacode_cli.teammates.registry import TeammateRegistry
from novacode_cli.teams.models import TeamError
from tests.test_teammates import FakeComputer

if TYPE_CHECKING:
    from pathlib import Path


class OwnedComputer(FakeComputer):
    def __init__(self, manifest: Path, owner: str) -> None:
        super().__init__()
        self.manifest = manifest
        self.owner = owner


@pytest.fixture
async def registry(tmp_path: Path):
    value = TeammateRegistry(
        tmp_path, controller_factory=partial(TeammateController, computer_factory=OwnedComputer)
    )
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def test_multiple_agents_recover_separate_data_and_computers(
    registry: TeammateRegistry, tmp_path: Path
):
    ada = await registry.command("create", {"name": "Ada"})
    bo = await registry.command("create", {"name": "Bo"})
    a = registry.agents[ada["agent_id"]]
    b = registry.agents[bo["agent_id"]]
    assert a.data["thread"] != b.data["thread"]
    assert a.computer.owner != b.computer.owner
    assert a.computer.manifest != b.computer.manifest
    for record in (ada, bo):
        await registry.command("pause", {"agent_id": record["agent_id"]})
        await registry.command("assign", {"agent_id": record["agent_id"], "text": record["name"]})
    a.data["notes"] = "Ada only"
    a.save()
    await registry.close()
    recovered = TeammateRegistry(tmp_path, controller_factory=registry.factory)
    await recovered.open()
    try:
        states = (await recovered.status())["agents"]
        assert {state["agent_id"] for state in states} == {ada["agent_id"], bo["agent_id"]}
        assert all(state["paused"] for state in states)
        assert {state["name"]: state["tasks"][0]["text"] for state in states} == {
            "Ada": "Ada",
            "Bo": "Bo",
        }
        assert recovered.agents[ada["agent_id"]].data["notes"] == "Ada only"
        assert recovered.agents[bo["agent_id"]].data["notes"] == ""
    finally:
        await recovered.close()


async def test_auto_approve_requires_explicit_agent_and_is_isolated(registry: TeammateRegistry):
    ada = await registry.command("create", {"name": "Ada"})
    with pytest.raises(TeamError, match="agent_id"):
        await registry.command("set_auto_approve", {"auto_approve": True})
    bo = await registry.command("create", {"name": "Bo"})
    await registry.command("set_auto_approve", {"agent_id": ada["agent_id"], "auto_approve": True})
    assert registry.agents[ada["agent_id"]].data["auto_approve"] is True
    assert registry.agents[bo["agent_id"]].data["auto_approve"] is False


async def test_existing_single_agent_is_adopted_without_moving_its_data(tmp_path: Path):
    factory = partial(TeammateController, computer_factory=OwnedComputer)
    original = factory(tmp_path)
    await original.open()
    await original.command("create", {"name": "Existing Ada"})
    original.data.pop("auto_approve")  # Legacy records have no approval-policy field.
    thread = original.data["thread"]
    original.data["notes"] = "Existing notes"
    original.save()
    original_path = original.computer.manifest
    await original.close()
    registry = TeammateRegistry(tmp_path, controller_factory=factory)
    await registry.open()
    try:
        assert registry.agents[thread] is registry.legacy
        assert registry.legacy.computer.manifest == original_path
        assert registry.legacy.data["notes"] == "Existing notes"
        assert registry.legacy.data["auto_approve"] is False
        bo = await registry.command("create", {"name": "Bo"})
        assert len((await registry.status())["agents"]) == 2
        await registry.command("delete", {"agent_id": thread, "confirm": "Existing Ada"})
        assert registry.agents[bo["agent_id"]].data["name"] == "Bo"
        # Deleting the legacy agent must not release the worker's profile fence.
        duplicate = TeammateRegistry(tmp_path, controller_factory=factory)
        with pytest.raises(TeamError, match="live coordinator"):
            await duplicate.open()
    finally:
        await registry.close()


@pytest.mark.parametrize("selector", [None, "../other-agent", "foreign", {}, 123])
async def test_mutations_require_an_exact_agent_when_multiple_exist(
    registry: TeammateRegistry, selector: object
):
    await registry.command("create", {"name": "Ada"})
    await registry.command("create", {"name": "Bo"})
    with pytest.raises(TeamError, match="Choose a teammate"):
        await registry.command("assign", {"agent_id": selector, "text": "Wrong target"})
    assert all(not controller.data["tasks"] for controller in registry.agents.values())


async def test_approval_and_delete_are_bound_to_one_agent(registry: TeammateRegistry):
    ada = await registry.command("create", {"name": "Ada"})
    bo = await registry.command("create", {"name": "Bo"})
    a = registry.agents[ada["agent_id"]]
    b = registry.agents[bo["agent_id"]]
    a.data["approvals"] = [{"id": "ada-input", "state": "pending"}]
    a.save()
    with pytest.raises(TeamError, match="stale"):
        await registry.command(
            "decide",
            {
                "agent_id": bo["agent_id"],
                "id": "ada-input",
                "approve": True,
            },
        )
    assert a.data["approvals"][0]["state"] == "pending"
    await registry.command("delete", {"agent_id": bo["agent_id"], "confirm": "Bo"})
    assert b.closed
    assert not a.closed
    assert (await registry.status())["agents"][0]["name"] == "Ada"


async def test_slow_control_for_one_agent_does_not_block_another(registry: TeammateRegistry):
    ada = await registry.command("create", {"name": "Ada"})
    bo = await registry.command("create", {"name": "Bo"})
    await registry.command("pause", {"agent_id": bo["agent_id"]})
    a = registry.agents[ada["agent_id"]]
    original_call = a.computer.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(operation: str, **arguments: object) -> dict:
        if operation == "takeover":
            entered.set()
            await release.wait()
        return await original_call(operation, **arguments)

    a.computer.call = blocked
    takeover = asyncio.create_task(registry.command("takeover", {"agent_id": ada["agent_id"]}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        result = await asyncio.wait_for(
            registry.command("assign", {"agent_id": bo["agent_id"], "text": "Bo work"}), 1
        )
        assert result["tasks"][0]["text"] == "Bo work"
        assert not a.data["tasks"]
    finally:
        release.set()
        await takeover


async def test_deleted_agents_do_not_reappear_after_restart(
    registry: TeammateRegistry, tmp_path: Path
):
    ada = await registry.command("create", {"name": "Ada"})
    await registry.command("delete", {"agent_id": ada["agent_id"], "confirm": "Ada"})
    await registry.close()
    recovered = TeammateRegistry(tmp_path, controller_factory=registry.factory)
    await recovered.open()
    try:
        assert (await recovered.status())["agents"] == []
    finally:
        await recovered.close()


@pytest.mark.timeout(30)
async def test_service_routes_separate_previews_and_lists_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from urllib.parse import parse_qs, urlsplit

    from novacode_cli.teammates import service
    from tests.test_teammates import until

    factory = partial(TeammateController, computer_factory=OwnedComputer)
    monkeypatch.setattr(service, "profile_directory", lambda: tmp_path)
    monkeypatch.setattr(
        service, "TeammateRegistry", partial(TeammateRegistry, controller_factory=factory)
    )
    worker = asyncio.create_task(service.serve(tmp_path))
    try:
        await until((tmp_path / "service.json").exists)
        ada = await service.request("create", {"name": "Ada"}, launch=False)
        bo = await service.request("create", {"name": "Bo"}, launch=False)
        roster = await service.request("status", launch=False)
        assert {a["name"] for a in roster["agents"]} == {"Ada", "Bo"}
        with pytest.raises(TeamError, match="Choose a teammate"):
            await service.request("preview", launch=False)
        a_preview = await service.request("preview", {"agent_id": ada["agent_id"]}, launch=False)
        await service.request(
            "set_auto_approve", {"agent_id": ada["agent_id"], "auto_approve": True}, launch=False
        )
        b_preview = await service.request("preview", {"agent_id": bo["agent_id"]}, launch=False)
        assert a_preview["url"] != b_preview["url"]
        parsed = urlsplit(a_preview["url"])
        headers = {
            "X-Nova-Token": parse_qs(parsed.query)["token"][0],
            "X-Nova-Request": "1",
        }
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{parsed.port}/status", headers=headers
            ) as response:
                assert (await response.json())["auto_approve"] is True
            async with client.post(
                f"http://127.0.0.1:{parsed.port}/set_auto_approve", headers=headers
            ) as response:
                assert response.status == 404
        async with (
            aiohttp.ClientSession() as client,
            client.post(f"http://127.0.0.1:{parsed.port}/takeover", headers=headers) as response,
        ):
            assert response.status == 200
            assert (await response.json())["manual"]
        roster = await service.request("status", launch=False)
        states = {a["name"]: a for a in roster["agents"]}
        assert states["Ada"]["computer"]["manual"]
        assert not states["Bo"]["computer"]["manual"]
        await service.request("shutdown", launch=False)
        await worker
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.timeout(45)
async def test_two_real_graphs_keep_private_conversations_and_notes(
    registry: TeammateRegistry, monkeypatch: pytest.MonkeyPatch
):
    import importlib

    from langchain_core.messages import AIMessage

    from novacode_cli.config import model_create
    from tests.test_team_inprocess import ScriptedModel
    from tests.test_teammates import until

    await asyncio.to_thread(importlib.import_module, "novacode_cli.agents.core_agent")

    def model(_provider: str, model_id: str) -> ScriptedModel:
        return ScriptedModel(responses=[AIMessage(f"Answer from {model_id}")])

    monkeypatch.setattr(model_create, "build_chat_model", model)
    ada = await registry.command(
        "create",
        {
            "name": "Ada",
            "model": {"provider": "openai", "model": "gpt-4o"},
        },
    )
    bo = await registry.command(
        "create",
        {
            "name": "Bo",
            "model": {"provider": "openai", "model": "gpt-4o-mini"},
        },
    )
    await asyncio.gather(
        registry.command("assign", {"agent_id": ada["agent_id"], "text": "Ada private task"}),
        registry.command("assign", {"agent_id": bo["agent_id"], "text": "Bo private task"}),
    )
    await until(
        lambda: all(c.data["tasks"][0]["status"] == "completed" for c in registry.agents.values())
    )
    for record, own, other in ((ada, "Ada", "Bo"), (bo, "Bo", "Ada")):
        controller = registry.agents[record["agent_id"]]
        await controller.runner
        graph = controller.handle.agent
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": controller.data["thread"]}}
        )
        text = str(snapshot.values["messages"])
        assert f"{own} private task" in text
        assert f"{other} private task" not in text
        notes = graph.nodes["tools"].bound._tools_by_name["private_notes"]
        await notes.ainvoke({"text": f"{own} private notes"})
        assert controller.data["notes"] == f"{own} private notes"
        assert controller.computer.phase == "suspended"
    assert registry.agents[ada["agent_id"]].data["notes"] == "Ada private notes"


async def test_shutdown_cannot_accept_a_new_agent_or_resume_work(registry: TeammateRegistry):
    ada = await registry.command("create", {"name": "Ada"})
    controller = registry.agents[ada["agent_id"]]
    original_call = controller.computer.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(operation: str, **arguments: object) -> dict:
        if operation == "stop":
            entered.set()
            await release.wait()
        return await original_call(operation, **arguments)

    controller.computer.call = blocked
    stopping = asyncio.create_task(registry.stop())
    await asyncio.wait_for(entered.wait(), 1)
    creating = asyncio.create_task(registry.command("create", {"name": "Too late"}))
    try:
        assert (await registry.status())["worker"] == "stopping"
        with pytest.raises(TeamError, match="stopping"):
            await registry.command("resume", {"agent_id": ada["agent_id"]})
    finally:
        release.set()
        await stopping
    with pytest.raises(TeamError, match="stopping"):
        await creating
    assert len(registry.agents) == 1
