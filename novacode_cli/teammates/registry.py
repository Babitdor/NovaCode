"""One profile worker owns independent, retained teammate controllers."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from novacode_cli.teammates.controller import TeammateController
from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class TeammateRegistry:
    """Keep the original profile fence and recover each agent in its own directory."""

    def __init__(
        self,
        directory: Path,
        *,
        controller_factory: Callable[[Path], TeammateController] = TeammateController,
    ) -> None:
        """Defer all storage and runtime access until open."""
        self.directory = directory
        self.factory = controller_factory
        self.legacy = self.factory(directory)
        self.agents: dict[str, TeammateController] = {}
        self.lock = asyncio.Lock()
        self.closing = False

    async def open(self) -> None:
        """Adopt existing single-agent data in place, then recover additional agents."""
        await self.legacy.open()  # The original fence also excludes older workers.
        if self.legacy.data.get("name"):
            self.agents[self.legacy.data["thread"]] = self.legacy
        try:
            root = self._agent_root()
            for path in sorted(root.iterdir()):
                if (
                    not path.is_dir()
                    or path.is_symlink()
                    or path.resolve().parent != root.resolve()
                    or not self._valid_id(path.name)
                ):
                    continue
                controller = self.factory(path)
                try:
                    await controller.open()
                except BaseException:
                    await controller.close()
                    raise
                if controller.data.get("name"):
                    self.agents[path.name] = controller
                else:
                    await controller.close()
        except BaseException:
            await self.close()
            raise

    def _agent_root(self) -> Path:
        root = self.directory / "agents"
        root.mkdir(exist_ok=True)
        if root.is_symlink() or root.resolve().parent != self.directory.resolve():
            message = "Teammate storage must stay inside its owned profile directory"
            raise TeamError(message)
        return root

    @staticmethod
    def _valid_id(value: str) -> bool:
        try:
            return uuid.UUID(hex=value).hex == value
        except ValueError:
            return False

    def select(self, agent_id: object = None) -> tuple[str, TeammateController]:
        """Route only by an exact registered identity; names and paths are never selectors."""
        if self.closing:
            message = "Teammate worker is stopping"
            raise TeamError(message)
        if agent_id is None and len(self.agents) == 1:
            return next(iter(self.agents.items()))
        if not isinstance(agent_id, str) or agent_id not in self.agents:
            message = "Choose a teammate by agent_id"
            raise TeamError(message)
        return agent_id, self.agents[agent_id]

    async def status(self) -> dict:
        """Expose the full roster; preserve single-agent status for existing clients."""
        agents = [
            {**await controller.status(), "agent_id": agent_id}
            for agent_id, controller in list(self.agents.items())
        ]
        return {
            **(agents[0] if len(agents) == 1 else {"computer": None}),
            "agents": agents,
            "worker": "stopping" if self.closing else "running",
        }

    async def command(self, action: str, payload: dict) -> dict:
        """Serialize roster mutations and leave task execution to each controller."""
        payload = dict(payload)
        agent_id = payload.pop("agent_id", None)
        if action == "status" and agent_id is None:
            return await self.status()
        if action not in {"create", "delete"}:
            agent_id, controller = self.select(agent_id)
            result = await controller.command(action, payload)
            return {**result, "agent_id": agent_id}
        async with self.lock:
            if self.closing:
                message = "Teammate worker is stopping"
                raise TeamError(message)
            if action == "create":
                if agent_id is not None:
                    message = "Creation cannot target an existing teammate"
                    raise TeamError(message)
                agent_id = uuid.uuid4().hex
                path = self._agent_root() / agent_id
                path.mkdir()
                controller = self.factory(path)
                try:
                    await controller.open()
                    result = await controller.command("create", payload)
                except BaseException:
                    await controller.close()
                    raise
                self.agents[agent_id] = controller
            else:
                agent_id, controller = self.select(agent_id)
                result = await controller.command(action, payload)
                if action == "delete":
                    if controller is not self.legacy:
                        await controller.close()
                    del self.agents[agent_id]
            return {**result, "agent_id": agent_id}

    async def stop(self) -> None:
        """Stop every teammate before shutting down the worker."""
        async with self.lock:
            self.closing = True
            try:
                results = await asyncio.gather(
                    *(controller.command("stop", {}) for controller in self.agents.values()),
                    return_exceptions=True,
                )
            except BaseException:
                self.closing = False
                raise
            for result in results:
                if isinstance(result, BaseException):
                    self.closing = False
                    raise result

    async def close(self) -> None:
        """Attempt all shutdowns before releasing the profile fence."""
        async with self.lock:
            self.closing = True
            results = await asyncio.gather(
                *(
                    controller.close()
                    for controller in self.agents.values()
                    if controller is not self.legacy
                ),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            await self.legacy.close()
