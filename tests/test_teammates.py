"""Durable profile worker invariants without providers, host tools, or QEMU."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiohttp
import pytest
from langchain_core.messages import AIMessage

from novacode_cli.teammates.controller import TeammateController
from novacode_cli.teams.models import TeamError
from tests.test_team_inprocess import ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class FakeComputer:
    def __init__(self, *_args: object) -> None:
        self.manual = False
        self.phase = "stopped"
        self.actions: list[dict] = []
        self.fail = False

    async def call(self, operation: str, **arguments: object):
        if operation == "start":
            self.phase = "ready"
        elif operation == "stop":
            self.phase = "suspended"
        elif operation == "takeover":
            self.manual = True
        elif operation == "return_control":
            self.manual = False
        elif operation == "action":
            if self.fail:
                message = "Input completion unknown"
                raise RuntimeError(message)
            self.actions.append(arguments)
            return [{"type": "text", "text": "shot"}]
        elif operation == "screenshot":
            return [{"type": "text", "text": "shot"}]
        elif operation == "approval_frame":
            return b"test-png"
        return {"phase": self.phase, "manual": self.manual}

    def close(self) -> bool:
        return True


@pytest.fixture
async def controller(tmp_path: Path):
    value = TeammateController(tmp_path, computer_factory=FakeComputer)
    await value.open()
    await value.command("create", {"name": "Ada"})
    value.data["paused"] = True
    value.save()
    try:
        yield value
    finally:
        await value.close()


async def until(predicate: Callable[[], bool]):
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - Poll durable state, not an in-process signal.
            await asyncio.sleep(0.01)


async def test_model_edit_preserves_identity_and_survives_restart(
    controller: TeammateController, tmp_path: Path
):
    from unittest.mock import AsyncMock

    from novacode_cli.config.nova_config import NovaConfig

    original = NovaConfig().get_model_config()
    thread = controller.data["thread"]
    computer = controller.computer
    controller.data["notes"] = "Keep my private notes"
    await controller.command("assign", {"text": "Retained assignment"})
    handle = SimpleNamespace(stop=AsyncMock(return_value=True))
    controller.handle = handle
    selection = {"provider": "openai", "model": "custom-model-id"}
    vision = {"provider": "anthropic", "model": "custom-vision-id"}
    await controller.command("edit", {"model": selection, "vision": vision})
    handle.stop.assert_awaited_once()
    assert controller.handle is None
    assert controller.computer is computer
    assert NovaConfig().get_model_config() == original
    await controller.close()
    recovered = TeammateController(tmp_path, computer_factory=FakeComputer)
    await recovered.open()
    try:
        assert recovered.data["model"] == selection
        assert recovered.data["vision"] == vision
        assert recovered.data["thread"] == thread
        assert recovered.data["notes"] == "Keep my private notes"
        assert recovered.data["tasks"][0]["text"] == "Retained assignment"
        assert recovered.data["paused"]
    finally:
        await recovered.close()


@pytest.mark.parametrize(
    "selection",
    [
        {"provider": "unknown", "model": "test"},
        {"provider": "openai", "model": " "},
        {"provider": "openai", "model": "test", "api_key": "not-accepted"},
        None,
    ],
)
async def test_invalid_model_edit_is_atomic(controller: TeammateController, selection: dict | None):
    import copy

    original = copy.deepcopy(controller.data)
    with pytest.raises(TeamError):
        await controller.command("edit", {"name": "Changed", "model": selection})
    assert controller.data == original


async def test_model_edit_requires_execution_to_stop(controller: TeammateController):
    controller.runner = asyncio.create_task(asyncio.Event().wait())
    with pytest.raises(TeamError, match="Pause before editing"):
        await controller.command("edit", {"model": {"provider": "openai", "model": "new"}})


async def test_create_accepts_explicit_model_and_vision(tmp_path: Path):
    value = TeammateController(tmp_path, computer_factory=FakeComputer)
    await value.open()
    selection = {"provider": "openai", "model": "custom-model"}
    try:
        result = await value.command(
            "create", {"name": "Ada", "model": selection, "vision": selection}
        )
        assert result["model"] == selection
        assert result["vision"] == selection
    finally:
        await value.close()


async def test_queued_assignment_is_durable_and_restart_needs_resume(
    controller: TeammateController, tmp_path: Path
):
    await controller.command("assign", {"text": "Inspect browser"})
    original = controller.data["thread"]
    await controller.close()
    recovered = TeammateController(tmp_path, computer_factory=FakeComputer)
    await recovered.open()
    try:
        assert recovered.data["thread"] == original
        assert recovered.data["tasks"][0]["text"] == "Inspect browser"
        assert recovered.data["paused"] is True
        assert recovered.runner is None
    finally:
        await recovered.close()


async def test_duplicate_controllers_cannot_acquire_owner(
    controller: TeammateController, tmp_path: Path
):
    assert controller.data
    duplicate = TeammateController(tmp_path, computer_factory=FakeComputer)
    with pytest.raises(TeamError, match="live coordinator"):
        await duplicate.open()


async def test_input_requires_durable_exact_single_use_approval(controller: TeammateController):
    controller.data["paused"] = False
    controller.active = {"id": "task"}
    pending = asyncio.create_task(
        controller.desktop_action(
            {
                "action": "type",
                "screenshot_id": "shot-1",
                "text": "hello",
            }
        )
    )
    await until(lambda: bool(controller.data["approvals"]))
    proposal = controller.data["approvals"][-1]
    stored = controller.coordinator._store.teammate_state(controller.identity)
    assert stored["approvals"][-1]["args"]["text"] == "hello"
    assert controller.computer.actions == []
    await controller.command("decide", {"id": proposal["id"], "approve": True})
    await pending
    assert proposal["state"] == "completed"
    assert len(controller.computer.actions) == 1
    with pytest.raises(TeamError, match="stale"):
        await controller.command("decide", {"id": proposal["id"], "approve": True})


@pytest.mark.parametrize("invalidate", ["deny", "observe", "takeover"])
async def test_denied_or_stale_input_never_executes(
    controller: TeammateController, invalidate: str
):
    controller.data["paused"] = False
    controller.active = {"id": "task"}
    pending = asyncio.create_task(
        controller.desktop_action({"action": "key", "screenshot_id": "s", "keys": ["enter"]})
    )
    await until(lambda: bool(controller.data["approvals"]))
    proposal = controller.data["approvals"][-1]
    if invalidate == "deny":
        await controller.command("decide", {"id": proposal["id"], "approve": False})
    elif invalidate == "observe":
        await controller.observe()
    else:
        await controller.command("takeover", {})
        await controller.command("return_control", {})
        assert controller.data["paused"] is True
    with pytest.raises(TeamError, match="denied or invalidated"):
        await pending
    assert controller.computer.actions == []


async def test_uncertain_input_persists_and_is_not_replayed(
    controller: TeammateController, tmp_path: Path
):
    controller.data["paused"] = False
    controller.active = {"id": "task"}
    controller.computer.fail = True
    pending = asyncio.create_task(
        controller.desktop_action({"action": "click", "screenshot_id": "s"})
    )
    await until(lambda: bool(controller.data["approvals"]))
    proposal = controller.data["approvals"][-1]
    await controller.command("decide", {"id": proposal["id"], "approve": True})
    with pytest.raises(RuntimeError, match="unknown"):
        await pending
    assert proposal["state"] == "uncertain"
    await controller.close()
    recovered = TeammateController(tmp_path, computer_factory=FakeComputer)
    await recovered.open()
    try:
        assert recovered.data["approvals"][-1]["state"] == "uncertain"
        assert recovered.computer.actions == []
        assert recovered.data["paused"] is True
    finally:
        await recovered.close()


async def test_recovery_invalidates_offline_approval(
    controller: TeammateController, tmp_path: Path
):
    controller.data["approvals"] = [
        {"id": "pending", "state": "pending"},
        {"id": "issued", "state": "issued"},
    ]
    controller.save()
    await controller.close()
    recovered = TeammateController(tmp_path, computer_factory=FakeComputer)
    await recovered.open()
    try:
        assert [a["state"] for a in recovered.data["approvals"]] == ["stale", "uncertain"]
        with pytest.raises(TeamError, match="stale"):
            await recovered.command("decide", {"id": "pending", "approve": True})
    finally:
        await recovered.close()


async def test_real_graph_keeps_private_sqlite_conversation_and_excludes_host_tools(
    controller: TeammateController, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.config import model_create

    # Cold optional imports are setup, not part of this persistence check's
    # background completion deadline. Production also builds this off-loop.
    await asyncio.to_thread(importlib.import_module, "novacode_cli.agents.core_agent")

    selections = []

    def build_model(provider: str, model: str) -> ScriptedModel:
        selections.append((provider, model))
        return ScriptedModel(responses=[AIMessage("Inspected.")])

    monkeypatch.setattr(model_create, "build_chat_model", build_model)
    controller.data["model"] = {"provider": "openai", "model": "gpt-4o"}
    await controller.command("assign", {"text": "First task @C:/private/secret.txt"})
    await controller.command("assign", {"text": "Second task"})
    await controller.command("resume", {})
    await until(lambda: all(t["status"] == "completed" for t in controller.data["tasks"]))
    await controller.runner
    assert controller.computer.phase == "suspended"
    assert all(t.get("usage") for t in controller.data["tasks"])
    graph = controller.handle.agent
    node = graph.nodes["tools"].bound
    assert set(node._tools_by_name) == {
        "computer_status",
        "computer_screenshot",
        "computer_action",
        "private_notes",
    }
    snapshot = await graph.aget_state({"configurable": {"thread_id": controller.data["thread"]}})
    text = str(snapshot.values["messages"])
    assert "@C:/private/secret.txt" in text
    assert "First task" in text
    assert "Second task" in text
    assert controller.data["thread"] not in str(controller.computer.actions)
    await controller.command(
        "edit", {"model": {"provider": "anthropic", "model": "claude-sonnet-4-5-20250929"}}
    )
    await controller.command("assign", {"text": "Third task with a new provider"})
    await until(lambda: controller.data["tasks"][-1]["status"] == "completed")
    await controller.runner
    assert selections[-1] == ("anthropic", "claude-sonnet-4-5-20250929")
    snapshot = await controller.handle.agent.aget_state(
        {"configurable": {"thread_id": controller.data["thread"]}}
    )
    text = str(snapshot.values["messages"])
    assert "First task" in text
    assert "Third task with a new provider" in text


async def test_delete_requires_name_and_failed_cleanup_retains_records(
    controller: TeammateController,
):
    with pytest.raises(TeamError, match="Confirm"):
        await controller.command("delete", {"confirm": "wrong"})
    original = controller.computer.call

    async def failed(operation: str, **arguments: object) -> dict:
        if operation == "delete":
            message = "cleanup failed"
            raise RuntimeError(message)
        return await original(operation, **arguments)

    controller.computer.call = failed
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await controller.command("delete", {"confirm": "Ada"})
    assert controller.data["name"] == "Ada"
    with pytest.raises(TeamError, match="deletion is incomplete"):
        await controller.command("resume", {})
    assert controller.coordinator._store.teammate_state(controller.identity)["name"] == "Ada"


async def test_loopback_reconnect_and_origin_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.teammates import service

    monkeypatch.setattr(service, "profile_directory", lambda: tmp_path)
    worker = asyncio.create_task(service.serve(tmp_path))
    try:
        await until((tmp_path / "service.json").exists)
        endpoint = json.loads((tmp_path / "service.json").read_text())
        assert (await service.request("status", launch=False))["computer"] is None
        async with aiohttp.ClientSession() as client:
            url = f"http://127.0.0.1:{endpoint['port']}/control/status"
            for headers in (
                {},
                {
                    "X-Nova-Token": endpoint["token"],
                    "X-Nova-Request": "1",
                    "Origin": "https://evil.example",
                },
                {"X-Nova-Token": endpoint["token"], "X-Nova-Request": "1", "Host": "evil.example"},
            ):
                async with client.post(url, json={}, headers=headers) as response:
                    assert response.status == 403
        await service.request("shutdown", launch=False)
        await worker
        assert not (tmp_path / "service.json").exists()
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def test_reused_pid_endpoint_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.teammates import service

    (tmp_path / "service.json").write_text(json.dumps({"pid": 12, "born": 1, "port": 1234}))
    monkeypatch.setattr(
        service.psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: 2)
    )
    assert service.endpoint(tmp_path) is None


async def test_panel_connects_without_owning_worker(monkeypatch: pytest.MonkeyPatch):
    from textual.app import App

    from novacode_cli.tui import teammates

    calls = []

    async def fake_request(action: str, payload: dict | None = None) -> dict:
        calls.append((action, payload))
        return {"name": "Ada", "role": "Assistant", "computer": {"phase": "suspended"}, "tasks": []}

    monkeypatch.setattr(teammates, "request", fake_request)
    app = App()
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(teammates.TeammatesScreen())
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.panels["legacy"].query_one("#teammate-name").value == "Ada"
        await pilot.press("escape")
    assert all(action == "status" for action, _ in calls)


async def test_paused_graph_recovers_pending_tool_without_replaying_input(
    controller: TeammateController, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.config import model_create

    responses = [
        [
            AIMessage(
                "",
                tool_calls=[
                    {
                        "id": "interrupted",
                        "name": "computer_action",
                        "args": {"action": "click", "screenshot_id": "s"},
                    }
                ],
            )
        ],
        [AIMessage("Inspected current state; no replay.")],
    ]
    monkeypatch.setattr(
        model_create, "build_chat_model", lambda *_a: ScriptedModel(responses=responses.pop(0))
    )
    controller.data["model"] = {"provider": "openai", "model": "gpt-4o"}
    await controller.command("assign", {"text": "Inspect then click"})
    await controller.command("resume", {})
    await until(lambda: bool(controller.data["approvals"]))
    await controller.command("pause", {})
    assert controller.data["tasks"][0]["status"] == "paused"
    assert controller.data["approvals"][0]["state"] == "stale"
    await controller.command("resume", {})
    await until(lambda: controller.data["tasks"][0]["status"] in {"completed", "failed"})
    assert controller.data["tasks"][0]["status"] == "completed", controller.data["tasks"][0]
    await controller.runner
    assert controller.computer.actions == []
    snapshot = await controller.handle.agent.aget_state(
        {"configurable": {"thread_id": controller.data["thread"]}}
    )
    assert "never replay automatically" in str(snapshot.values["messages"])


async def test_forbidden_tool_calls_cannot_reach_host(
    controller: TeammateController, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.config import model_create

    forbidden = [
        "shell",
        "execute",
        "read_file",
        "evaluate",
        "tool_search",
        "task",
        "session_send",
        "memory_wiki",
    ]
    model = ScriptedModel(
        responses=[
            AIMessage(
                "", tool_calls=[{"id": name, "name": name, "args": {}} for name in forbidden]
            ),
            AIMessage("No host capability available."),
        ]
    )
    monkeypatch.setattr(model_create, "build_chat_model", lambda *_a: model)
    controller.data["model"] = {"provider": "openai", "model": "gpt-4o"}
    await controller.command("assign", {"text": "Try alternate entrypoints"})
    await controller.command("resume", {})
    await until(lambda: controller.data["tasks"][0]["status"] == "completed")
    await controller.runner
    snapshot = await controller.handle.agent.aget_state(
        {"configurable": {"thread_id": controller.data["thread"]}}
    )
    from langchain_core.messages import ToolMessage

    denied = [m for m in snapshot.values["messages"] if isinstance(m, ToolMessage)]
    assert len(denied) == len(forbidden)
    assert all("not a valid tool" in m.content for m in denied)
    assert controller.computer.actions == []


async def test_failed_vision_preflight_blocks_task_and_retains_usage(
    controller: TeammateController, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.config import model_create

    monkeypatch.setattr(
        model_create,
        "build_chat_model",
        lambda *_a: ScriptedModel(responses=[AIMessage("")]),
    )
    controller.data["model"] = {"provider": "ollama", "model": "text-only-model"}
    controller.data["vision"] = {"provider": "openai", "model": "gpt-4o"}
    await controller.command("assign", {"text": "Inspect browser"})
    await controller.command("resume", {})
    await until(lambda: controller.data["tasks"][0]["status"] == "failed")
    await controller.runner
    task = controller.data["tasks"][0]
    assert "Vision returned no desktop description" in task["error"]
    assert task["usage_history"]
    assert controller.data["paused"] is True
    assert controller.computer.actions == []
    assert controller.computer.phase == "suspended"


@pytest.mark.timeout(40)
async def test_detached_worker_reconnect_restart_and_duplicate_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.teammates import service

    monkeypatch.setattr(service, "profile_directory", lambda: tmp_path)
    script = (
        "import asyncio,sys; from pathlib import Path; "
        "from novacode_cli.teammates.service import serve; "
        "asyncio.run(serve(Path(sys.argv[1])))"
    )
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    children = []

    def launch() -> subprocess.Popen:
        child = subprocess.Popen(  # noqa: S603 - Fixed harness, isolated temporary profile.
            [sys.executable, "-c", script, str(tmp_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "USERPROFILE": str(tmp_path), "HOME": str(tmp_path)},
            **options,
        )
        children.append(child)
        return child

    try:
        first = await asyncio.to_thread(launch)
        await until((tmp_path / "service.json").exists)
        await service.request("create", {"name": "Persistent"}, launch=False)
        await service.request("pause", launch=False)
        await service.request("assign", {"text": "Durable queued work"}, launch=False)
        second = await asyncio.to_thread(launch)
        await until(lambda: second.poll() is not None)
        assert first.poll() is None
        one, two = await asyncio.gather(
            service.request("status", launch=False), service.request("status", launch=False)
        )
        assert one["thread"] == two["thread"]
        assert one["tasks"][0]["text"] == "Durable queued work"
        first.terminate()
        await asyncio.to_thread(first.wait, timeout=10)
        (tmp_path / "service.json").unlink()
        third = await asyncio.to_thread(launch)
        await until((tmp_path / "service.json").exists)
        recovered = await service.request("status", launch=False)
        assert recovered["thread"] == one["thread"]
        assert recovered["tasks"][0]["status"] == "queued"
        assert recovered["paused"] is True
        await service.request("shutdown", launch=False)
        await asyncio.to_thread(third.wait, timeout=10)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                await asyncio.to_thread(child.wait, timeout=10)
