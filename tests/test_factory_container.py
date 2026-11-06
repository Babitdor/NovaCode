"""Factory container mounts and ownership without a live Docker daemon."""

from __future__ import annotations

import asyncio
import runpy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from novacode_cli.prime_factory.container import (
    WORKER_BOOTSTRAP,
    FactoryContainerAdapter,
    FactoryContainerHandle,
    host_path,
)
from novacode_cli.sessions import worktree
from novacode_cli.teams.adapters.process import ProcessHandle
from novacode_cli.teams.models import (
    Identity,
    SpawnRequest,
    TeamError,
    UncertainStartupError,
    operation_digest,
)
from novacode_cli.teams.scheduler import PROCESS_BUDGET

PROFILE = {"image": "nova/worker@sha256:" + "a" * 64, "network": "bridge"}
MODEL = {"provider": "fake", "model": "fake", "allowed_tools": ["read_file", "edit_file"]}


@pytest.fixture
def factory_request(tmp_path: Path) -> SpawnRequest:
    source = tmp_path / "source"
    source.mkdir()
    for args in (
        ["init"],
        ["config", "user.name", "Offline"],
        ["config", "user.email", "offline@example.invalid"],
    ):
        assert worktree._git(args, cwd=source)[0] == 0
    (source / "code.py").write_text("value = 1\n")
    assert worktree._git(["add", "code.py"], cwd=source)[0] == 0
    assert worktree._git(["commit", "-m", "baseline"], cwd=source)[0] == 0
    task = tmp_path / "task"
    assert worktree._git(["worktree", "add", "-b", "factory-task", str(task)], cwd=source)[0] == 0
    return SpawnRequest(
        Identity(str(source), "run", "attempt", "run"),
        "task",
        "fork",
        "developer",
        task,
        factory={"run": "run", "task": "task", "attempt": "attempt", "role": "developer"},
    )


async def test_owned_task_mount_keeps_git_and_worktree_ownership_on_host(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    started = AsyncMock()
    monkeypatch.setattr(FactoryContainerHandle, "start", started)
    source = Path(factory_request.identity.project)
    original_git = (factory_request.cwd / ".git").read_bytes()
    handle = await asyncio.to_thread(
        FactoryContainerAdapter, MODEL, PROFILE, workspace=factory_request.cwd
    )
    handle = await handle.start(factory_request, AsyncMock(), AsyncMock())
    assert started.await_count == 1
    assert handle.worktree is None
    assert handle.request.mode == "remote"
    assert handle.request.cwd == factory_request.cwd
    assert handle.bootstrap["cwd"] == "/workspace"
    assert handle.revision == worktree._git(["rev-parse", "HEAD"], cwd=factory_request.cwd)[1]
    assert handle.recovery["kind"] == "container"
    assert handle.recovery["labels"]["nova.agent"] == "attempt"
    assert (factory_request.cwd / ".git").read_bytes() == original_git
    assert not (factory_request.cwd / ".nova-container-git").exists()
    assert worktree._git(["status", "--porcelain"], cwd=source)[1] == ""
    assert worktree._git(["status", "--porcelain"], cwd=factory_request.cwd)[1] == ""
    command = handle.container_command(["NOVA_FACTORY_BASE_REVISION", "OPENAI_API_KEY"])
    assert command[-3:] == ["python", "-c", WORKER_BOOTSTRAP]
    assert command.count("--mount") == 1
    mounts = [command[index + 1] for index, arg in enumerate(command) if arg == "--mount"]
    assert str(factory_request.cwd) in mounts[0]
    assert all(str(source) not in mount for mount in mounts)
    assert "--pull=never" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command


async def test_mapped_write_stays_bound_to_original_tool_arguments(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(FactoryContainerHandle, "start", AsyncMock())
    observed = []

    async def gateway(method: str, payload: dict) -> dict:
        observed.append((method, payload))
        return {
            "allowed": True,
            "digest": operation_digest(payload["tool"], payload["args"], payload["tool_call_id"]),
        }

    adapter = await asyncio.to_thread(
        FactoryContainerAdapter, MODEL, PROFILE, workspace=factory_request.cwd
    )
    handle = await adapter.start(factory_request, AsyncMock(), gateway)
    args = {"file_path": "/workspace/code.py", "old_string": "1", "new_string": "2"}
    payload = {
        "tool": "edit_file",
        "args": args,
        "tool_call_id": "write",
        "write_paths": ["/workspace/code.py"],
    }
    result = await handle.gateway("authorize", payload)
    assert result == {"allowed": True, "digest": operation_digest("edit_file", args, "write")}
    assert observed[0][1]["args"]["file_path"] == str(factory_request.cwd / "code.py")
    assert observed[0][1]["write_paths"] == [str(factory_request.cwd / "code.py")]
    assert payload["args"] == args
    with pytest.raises(TeamError, match="outside its workspace"):
        await handle.gateway("authorize", {**payload, "write_paths": ["/outside/code.py"]})
    assert len(observed) == 1


async def test_host_digest_mismatch_denies_mapped_write(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(FactoryContainerHandle, "start", AsyncMock())
    gateway = AsyncMock(return_value={"allowed": True, "digest": "foreign"})
    adapter = await asyncio.to_thread(
        FactoryContainerAdapter, MODEL, PROFILE, workspace=factory_request.cwd
    )
    handle = await adapter.start(factory_request, AsyncMock(), gateway)
    result = await handle.gateway(
        "authorize",
        {
            "tool": "edit_file",
            "args": {"file_path": "code.py"},
            "tool_call_id": "write",
            "write_paths": ["/workspace/code.py"],
        },
    )
    assert result["allowed"] is False


async def test_startup_uncertainty_preserves_owned_handle_and_task(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(FactoryContainerHandle, "start", AsyncMock(side_effect=TeamError("failed")))
    stopped = AsyncMock(return_value=False)
    monkeypatch.setattr(FactoryContainerHandle, "stop", stopped)
    adapter = await asyncio.to_thread(
        FactoryContainerAdapter, MODEL, PROFILE, workspace=factory_request.cwd
    )
    with pytest.raises(UncertainStartupError) as caught:
        await adapter.start(factory_request, AsyncMock(), AsyncMock())
    assert caught.value.handle.recovery["kind"] == "container"
    assert stopped.await_count == 1
    assert factory_request.cwd.is_dir()


async def test_verified_stop_releases_container_only_not_factory_worktree(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.teams.adapters import container

    monkeypatch.setattr(FactoryContainerHandle, "start", AsyncMock())
    adapter = await asyncio.to_thread(
        FactoryContainerAdapter, MODEL, PROFILE, workspace=factory_request.cwd
    )
    handle = await adapter.start(factory_request, AsyncMock(), AsyncMock())
    monkeypatch.setattr(container, "stop_owned_container", AsyncMock(return_value=True))
    monkeypatch.setattr(ProcessHandle, "stop", AsyncMock(return_value=True))
    key = f"container:{handle.bootstrap['attempt']}"
    before = PROCESS_BUDGET.used
    PROCESS_BUDGET.reserve(key)
    handle._reserved = True
    assert await handle.stop()
    assert PROCESS_BUDGET.used == before
    assert factory_request.cwd.is_dir()
    assert (factory_request.cwd / ".git").is_file()


async def test_readonly_planning_can_mount_source_without_write_capability(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(FactoryContainerHandle, "start", AsyncMock())
    source = Path(factory_request.identity.project)
    request = replace(factory_request, cwd=source, agent_type="requirements")
    adapter = await asyncio.to_thread(
        FactoryContainerAdapter,
        {**MODEL, "allowed_tools": ["read_file"]},
        PROFILE,
        workspace=source,
        readonly=True,
    )
    handle = await adapter.start(request, AsyncMock(), AsyncMock())
    command = handle.container_command([])
    assert "readonly" in command[command.index("--mount") + 1]
    with pytest.raises(TeamError, match="mutation capabilities"):
        await asyncio.to_thread(
            FactoryContainerAdapter, MODEL, PROFILE, workspace=source, readonly=True
        )


async def test_source_or_foreign_workspace_cannot_be_writable(
    factory_request: SpawnRequest, monkeypatch: pytest.MonkeyPatch
):
    started = AsyncMock()
    monkeypatch.setattr(FactoryContainerHandle, "start", started)
    source = Path(factory_request.identity.project)
    adapter = await asyncio.to_thread(FactoryContainerAdapter, MODEL, PROFILE, workspace=source)
    with pytest.raises(TeamError, match="host-owned"):
        await adapter.start(replace(factory_request, cwd=source), AsyncMock(), AsyncMock())
    adapter = await asyncio.to_thread(
        FactoryContainerAdapter, MODEL, PROFILE, workspace=factory_request.cwd
    )
    with pytest.raises(TeamError, match="host-owned"):
        await adapter.start(
            replace(factory_request, factory={"run": "foreign"}), AsyncMock(), AsyncMock()
        )
    assert started.await_count == 0


def test_pinned_image_and_host_path_boundaries_are_required(tmp_path: Path):
    with pytest.raises(TeamError, match="pinned"):
        FactoryContainerAdapter(MODEL, {"image": "worker:latest"}, workspace=tmp_path)
    for path in ["/workspace/../outside", "/workspace-other/code.py", "relative.py", "/workspace"]:
        with pytest.raises(TeamError, match=r"outside|escaped"):
            host_path(path, tmp_path)


@pytest.mark.parametrize(
    ("model", "profile", "message"),
    [
        ({"model": "fake"}, PROFILE, "exact configured provider"),
        ({"provider": "fake"}, PROFILE, "exact configured provider"),
        ({**MODEL, "auth_mode": "chatgpt"}, PROFILE, "cannot forward local ChatGPT"),
        (MODEL, {**PROFILE, "readonly": True}, "read-only container profile"),
    ],
)
def test_configured_route_failures_never_fall_back_to_host(
    tmp_path: Path, model: dict, profile: dict, message: str
):
    with pytest.raises(TeamError, match=message):
        FactoryContainerAdapter(model, profile, workspace=tmp_path)


async def test_non_git_planning_remains_available_in_readonly_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(FactoryContainerHandle, "start", AsyncMock())
    request = SpawnRequest(
        Identity(str(tmp_path), "run", "attempt", "run"),
        "task",
        "fork",
        "requirements",
        tmp_path,
        factory={"run": "run", "task": "task", "attempt": "attempt"},
    )
    adapter = await asyncio.to_thread(
        FactoryContainerAdapter,
        {**MODEL, "allowed_tools": ["read_file"]},
        PROFILE,
        workspace=tmp_path,
        readonly=True,
    )
    handle = await adapter.start(request, AsyncMock(), AsyncMock())
    assert handle.revision is None
    assert (
        "readonly"
        in handle.container_command([])[handle.container_command([]).index("--mount") + 1]
    )


def test_ephemeral_git_identity_does_not_touch_host_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    metadata = tmp_path / "ephemeral"
    monkeypatch.setenv("GIT_DIR", str(metadata))
    monkeypatch.setenv("NOVA_FACTORY_BASE_REVISION", "a" * 40)
    called = []
    monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: called.append((args, kwargs)))
    exec(WORKER_BOOTSTRAP, {})  # noqa: S102 - Execute only the fixed production bootstrap.
    assert worktree._git(["rev-parse", "HEAD"], cwd=tmp_path) == (0, "a" * 40, "")
    assert called == [(("novacode_cli.teams.worker",), {"run_name": "__main__"})]
    assert not (tmp_path / ".git").exists()
