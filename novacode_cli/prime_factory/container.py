"""Configured factory containers bound to host-owned task workspaces."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from novacode_cli.teams.adapters.container import ContainerHandle, container_argv
from novacode_cli.teams.models import (
    SpawnRequest,
    TeamError,
    UncertainStartupError,
    operation_digest,
)
from novacode_cli.teams.workspace import WRITE_TOOLS

if TYPE_CHECKING:
    from novacode_cli.teams.models import EventCallback, GatewayCallback


# The worker has no Git capability. Minimal ephemeral metadata lets its existing
# readiness handshake attest the host-selected revision without mounting host Git.
WORKER_BOOTSTRAP = """import os, pathlib, runpy
metadata = pathlib.Path(os.environ['GIT_DIR'])
metadata.mkdir(mode=0o700, parents=True, exist_ok=False)
(metadata / 'objects').mkdir()
(metadata / 'refs').mkdir()
(metadata / 'HEAD').write_text(os.environ.pop('NOVA_FACTORY_BASE_REVISION') + '\\n')
(metadata / 'config').write_text('[core]\\nrepositoryformatversion = 0\\nbare = false\\n')
runpy.run_module('novacode_cli.teams.worker', run_name='__main__')
"""


def host_path(raw: str, workspace: Path) -> Path:
    """Translate only canonical container workspace destinations into the host lease."""
    path = PurePosixPath(raw)
    base = PurePosixPath("/workspace")
    if not path.is_absolute() or not path.is_relative_to(base) or ".." in path.parts:
        msg = "Container write destination is outside its workspace"
        raise TeamError(msg)
    destination = (workspace / path.relative_to(base).as_posix()).resolve()
    if destination == workspace or not destination.is_relative_to(workspace):
        msg = "Container write destination escaped its host workspace"
        raise TeamError(msg)
    return destination


class FactoryContainerHandle(ContainerHandle):
    """Use existing container ownership and recovery without owning its worktree."""

    def __init__(
        self,
        request: SpawnRequest,
        model: dict[str, Any],
        event: EventCallback,
        gateway: GatewayCallback,
        profile: dict[str, Any],
        revision: str | None,
    ) -> None:
        """Retain host ownership of all task branches and Git metadata."""
        super().__init__(request, model, event, gateway, None, profile)
        self.revision = revision

    def container_environment(self) -> dict[str, str]:
        """Give Git only ephemeral identity metadata, never the host's common directory."""
        return {
            **super().container_environment(),
            "GIT_DIR": "/tmp/nova-factory-git",  # noqa: S108 - Per-container tmpfs.
            "GIT_WORK_TREE": "/workspace",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "NOVA_FACTORY_BASE_REVISION": self.revision or "ref: refs/heads/factory-unborn",
        }

    def container_command(self, environment_names: list[str]) -> list[str]:
        """Keep resource, network, mount and image policy from the existing adapter."""
        command = super().container_command(environment_names)
        return [*command[:-3], "python", "-c", WORKER_BOOTSTRAP]


class FactoryContainerAdapter:
    """Mount a host-selected lease; never allocate, replace, or delete Git worktrees."""

    def __init__(
        self,
        model: dict[str, Any],
        profile: dict[str, Any],
        *,
        workspace: Path,
        readonly: bool = False,
    ) -> None:
        """Accept workspace and write capability only from the host scheduler."""
        self.model, self.profile = model, dict(profile)
        self.workspace = workspace.resolve()
        self.readonly = readonly
        if readonly:
            self.profile["readonly"] = True
        container_argv(self.profile, self.workspace, "validation", {}, [])
        if readonly and set(model.get("allowed_tools", [])) & (
            WRITE_TOOLS | {"shell", "execute", "execute_code", "bash"}
        ):
            msg = "Read-only factory containers cannot grant mutation capabilities"
            raise TeamError(msg)
        if not readonly and self.profile.get("readonly") is True:
            msg = "Configured read-only container profile cannot implement a factory task"
            raise TeamError(msg)
        if not model.get("provider") or not model.get("model"):
            msg = "Factory containers require the exact configured provider and model"
            raise TeamError(msg)
        if model.get("auth_mode") == "chatgpt":
            msg = (
                "Container mode cannot forward local ChatGPT sign-in; no provider fallback started"
            )
            raise TeamError(msg)

    async def start(
        self, request: SpawnRequest, event: EventCallback, gateway: GatewayCallback
    ) -> FactoryContainerHandle:
        """Validate the task binding before starting the configured image, with no fallback."""
        from novacode_cli.sessions.worktree import _git, repo_root

        factory = request.factory
        root = await asyncio.to_thread(request.cwd.resolve)
        source = await asyncio.to_thread(Path(request.identity.project).resolve)
        if (
            not factory
            or factory.get("run") != request.identity.root
            or factory.get("attempt") != request.identity.agent
            or root != self.workspace
            or (not self.readonly and root == source)
        ):
            msg = "Container workspace must match the host-owned factory task"
            raise TeamError(msg)
        if not self.readonly and not await asyncio.to_thread((root / ".git").is_file):
            msg = "Writable factory containers require an isolated linked worktree"
            raise TeamError(msg)
        if not self.readonly and await asyncio.to_thread(repo_root, root) != root:
            msg = "Factory container workspace must be a Git repository root"
            raise TeamError(msg)
        code, revision, _ = await asyncio.to_thread(_git, ["rev-parse", "HEAD"], cwd=root)
        if (code != 0 and not self.readonly) or (
            code == 0 and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision) is None
        ):
            msg = "Factory container requires an observed committed baseline"
            raise TeamError(msg)

        async def mapped_gateway(method: str, payload: dict[str, Any]) -> dict[str, Any]:
            if method != "authorize" or payload.get("tool") not in WRITE_TOOLS:
                return await gateway(method, payload)
            original = payload.get("args")
            supplied = payload.get("write_paths")
            if (
                not isinstance(original, dict)
                or not isinstance(supplied, list)
                or any(not isinstance(name, str) for name in supplied)
            ):
                msg = "Invalid container write destinations"
                raise TeamError(msg)
            args = dict(original)
            raw = args.get("file_path")
            if isinstance(raw, str) and (raw == "/workspace" or raw.startswith("/workspace/")):
                args["file_path"] = str(await asyncio.to_thread(host_path, raw, root))
            translated = {
                **payload,
                "args": args,
                "write_paths": [
                    str(await asyncio.to_thread(host_path, name, root)) for name in supplied
                ],
            }
            result = await gateway(method, translated)
            expected = operation_digest(payload["tool"], args, payload["tool_call_id"])
            if result.get("allowed") and result.get("digest") == expected:
                return {
                    **result,
                    "digest": operation_digest(payload["tool"], original, payload["tool_call_id"]),
                }
            return {"allowed": False, "reason": "Host rejected the mapped container write"}

        # Container recovery must retain its labels rather than the transient Docker CLI PID.
        request = replace(request, mode="remote")
        handle = FactoryContainerHandle(
            request,
            self.model,
            event,
            mapped_gateway,
            self.profile,
            revision if code == 0 else None,
        )
        try:
            await handle.start()
            return handle  # noqa: TRY300 - Keep launch and cleanup in one guard.
        except BaseException:
            if not await handle.stop():
                raise UncertainStartupError(handle) from None
            raise
