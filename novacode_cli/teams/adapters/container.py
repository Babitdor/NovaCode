"""Configured Nova workers in owned containers; no implicit provisioning."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from novacode_cli.teams.adapters.process import TeamWorktreeInfo
    from novacode_cli.teams.models import EventCallback, GatewayCallback

from novacode_cli.teams.adapters.process import ProcessHandle, _preserve_or_remove, strict_worktree
from novacode_cli.teams.models import SpawnRequest, TeamError, UncertainStartupError
from novacode_cli.teams.scheduler import PROCESS_BUDGET

MIN_CPUS, MAX_CPUS = 0.1, 16


def container_argv(
    profile: dict[str, Any],
    workspace: Path,
    name: str,
    labels: dict[str, str],
    environment_names: list[str],
) -> list[str]:
    """Build a non-TTY worker command from trusted configuration."""
    if "readonly" in profile and type(profile["readonly"]) is not bool:
        message = "Container readonly must be a JSON boolean"
        raise TeamError(message)
    image = profile.get("image", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]*@sha256:[a-f0-9]{64}", image):
        msg = "A trusted worker image pinned by sha256 digest is required"
        raise TeamError(msg)
    network = profile.get("network", "none")
    user = profile.get("user", "1000:1000")
    if network not in {"none", "bridge"} or not re.fullmatch(r"[1-9][0-9]*(?::[0-9]+)?", user):
        msg = "Use an explicit non-root user and none/bridge network"
        raise TeamError(msg)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", name):
        msg = "Invalid owned container name"
        raise TeamError(msg)
    if any(not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key) for key in environment_names):
        msg = "Invalid environment variable name"
        raise TeamError(msg)
    memory = str(profile.get("memory", "1g"))
    cpus = float(profile.get("cpus", 1))
    if not re.fullmatch(r"[1-9][0-9]*[mg]", memory) or not MIN_CPUS <= cpus <= MAX_CPUS:
        msg = "Invalid container resource limits"
        raise TeamError(msg)
    path = str(workspace.resolve())
    if "\n" in path or "\r" in path:
        msg = "Invalid workspace path"
        raise TeamError(msg)
    mount = io.StringIO()
    options = ["type=bind", f"src={path}", "dst=/workspace"]
    if profile.get("readonly") is True:
        options.append("readonly")
    csv.writer(mount).writerow(options)
    command = [
        "docker",
        "--context",
        "default",
        "run",
        "--pull=never",
        "--rm",
        "-i",
        "--name",
        name,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=256m",
        "--pids-limit=128",
        f"--memory={memory}",
        f"--cpus={cpus}",
        f"--network={network}",
        f"--user={user}",
        "--workdir=/workspace",
        "--mount",
        mount.getvalue().strip(),
    ]
    for key, value in labels.items():
        command.extend(["--label", f"{key}={value}"])
    for key in environment_names:
        command.extend(["--env", key])  # Names only; values never appear in argv.
    return [*command, image, "python", "-m", "novacode_cli.teams.worker"]


async def _docker(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "--context",
        "default",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", "Container control timed out"
    return (
        int(process.returncode or 0),
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def stop_owned_container(
    name: str,
    labels: dict[str, str],
    runner: Callable[..., Awaitable[tuple[int, str, str]]] = _docker,
) -> bool:
    """Verify ownership and termination before releasing a container lease."""
    try:
        code, output, error = await runner("inspect", "--format", "{{json .Config.Labels}}", name)
        if code != 0:
            return "No such container:" in error or "No such object:" in error
        actual = json.loads(output)
        if not isinstance(actual, dict) or any(
            actual.get(key) != value for key, value in labels.items()
        ):
            return False
        code, _, _ = await runner("stop", "--time", "5", name)
        if code != 0:
            return False
        code, output, error = await runner("inspect", "--format", "{{.State.Running}}", name)
        return (code == 0 and output.strip() == "false") or (
            code != 0 and ("No such container:" in error or "No such object:" in error)
        )
    except (OSError, ValueError, TypeError):
        return False


class ContainerAdapter:
    """No image pull, no profile mounts, and no implicit host-to-container scope expansion."""

    def __init__(self, model: dict[str, Any], profile: dict[str, Any]) -> None:
        """Accept only operator configuration, never raw model-selected launch commands."""
        self.model, self.profile = model, dict(profile)
        container_argv(self.profile, Path.cwd(), "validation", {}, [])

    async def start(
        self, request: SpawnRequest, event: EventCallback, gateway: GatewayCallback
    ) -> ContainerHandle:
        """Create a write-isolated workspace and a broker-bound Nova worker."""
        info = None
        handle = None
        git_metadata = None
        try:
            preparation = asyncio.create_task(
                asyncio.to_thread(strict_worktree, request.cwd, request.identity.agent)
            )
            try:
                info = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                info = await preparation
                raise
            request = replace(request, cwd=info.workspace)
            from novacode_cli.teams.container_git import prepare_git

            git_preparation = asyncio.create_task(asyncio.to_thread(prepare_git, info))
            try:
                git_metadata = await asyncio.shield(git_preparation)
            except asyncio.CancelledError:
                await git_preparation
                raise
            model = self.model.get("by_agent", {}).get(request.agent_type, self.model)
            if model.get("auth_mode") == "chatgpt":
                msg = (
                    "Container mode cannot forward local ChatGPT sign-in; "
                    "no provider fallback was started"
                )
                raise TeamError(msg)  # noqa: TRY301 - Rollback must include validation failures.
            if not model.get("provider") or not model.get("model"):
                msg = "Remote mode requires the exact configured provider and model"
                raise TeamError(msg)  # noqa: TRY301 - Rollback must include validation failures.
            handle = ContainerHandle(request, model, event, gateway, info, self.profile)
            handle.git_metadata = git_metadata
            await handle.start()
            return handle  # noqa: TRY300 - Keep launch and cleanup in one guard.
        except BaseException:
            if handle is not None:
                if not await handle.stop():
                    raise UncertainStartupError(handle) from None
            elif info is not None:
                await _preserve_or_remove(info.path, info.source)
            raise


class ContainerHandle(ProcessHandle):
    """A container lease survives client exit until container termination is verified."""

    def __init__(
        self,
        request: SpawnRequest,
        model: dict[str, Any],
        event: EventCallback,
        gateway: GatewayCallback,
        worktree: TeamWorktreeInfo | None,
        profile: dict[str, Any],
    ) -> None:
        """Bind recovery labels to this root and launch attempt."""
        super().__init__(request, model, event, gateway, worktree)
        self.profile = profile
        self.git_metadata: Path | None = None
        self.name = f"nova-team-{request.identity.agent}"
        self.labels = {
            "nova.agent": request.identity.agent,
            "nova.team": request.identity.team,
            "nova.root": request.identity.root,
            "nova.project": request.identity.project,
            "nova.attempt": self.bootstrap["attempt"],
        }
        self.bootstrap["cwd"] = "/workspace"
        self.supervisor._process_budget = None  # Reserve the container, not its transient CLI.
        self._reserved = False
        self.recovery = {"kind": "container", "name": self.name, "labels": self.labels}

    async def start(
        self,
        argv: list[str] | None = None,  # noqa: ARG002 - Adapter interface.
        *,
        env: dict[str, str] | None = None,  # noqa: ARG002 - Framework interface.
    ) -> None:
        """Forward only configured model credentials by environment name."""
        from novacode_cli.config.model_manager import MODEL_PRESETS, provider_api_key

        provider = self.bootstrap["provider"]
        preset = MODEL_PRESETS.get(provider, {})
        key_name = preset.get("api_key_var") or {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
        }.get(provider)
        values = {"HOME": "/tmp/nova-home"}  # noqa: S108 - Private per-container tmpfs.
        if key_name:
            from novacode_cli.config.credentials import get_stored_key

            key = (
                provider_api_key(provider)
                if provider in MODEL_PRESETS
                else (get_stored_key(key_name) or os.environ.get(key_name))
            )
            if key:
                values[key_name] = key
        for key in {"OLLAMA_HOST", preset.get("base_url_env")} - {None}:
            if isinstance(key, str) and key in os.environ:
                values[key] = os.environ[key]
        if self.git_metadata is not None:
            values.update(
                GIT_DIR="/workspace/.nova-container-git",
                GIT_WORK_TREE="/workspace",
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL="/dev/null",
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="safe.directory",
                GIT_CONFIG_VALUE_0="/workspace",
            )
        command = container_argv(
            self.profile, self.request.cwd, self.name, self.labels, sorted(values)
        )
        PROCESS_BUDGET.reserve(f"container:{self.bootstrap['attempt']}")
        self._reserved = True
        await super().start(command, env=values)

    async def _message(self, agent_id: str, message: dict[str, Any]) -> None:
        if message.get("t") == "exited" and self.ready.done() and not self.stopped:
            await self.event(
                "disconnected",
                {
                    "error": "Container client exited; worker termination is uncertain",
                    "recovery": self.recovery,
                },
            )
            return
        await super()._message(agent_id, message)

    async def stop(self) -> bool:
        """Do not remove a worktree or release capacity until the container is gone."""
        if not self._reserved:
            return True
        if self.child is not None:
            await self.supervisor._send(self.child, {"t": "shutdown"})
        if not await stop_owned_container(self.name, self.labels):
            return False
        certain = await super().stop()
        if certain and self._reserved:
            PROCESS_BUDGET.release(f"container:{self.bootstrap['attempt']}")
            self._reserved = False
        return certain
