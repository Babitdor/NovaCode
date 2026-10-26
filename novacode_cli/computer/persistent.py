"""Retained, explicitly owned local computers for background teammates."""
# ruff: noqa: ANN401, EM101, TRY003

from __future__ import annotations

import asyncio
import importlib
import json
import re
from typing import TYPE_CHECKING, Any

from novacode_cli.computer.runtime import Computer, ComputerError, _prepare_qemu_path
from novacode_cli.security.secret_files import write_secret_json

if TYPE_CHECKING:
    from pathlib import Path


class PersistentComputer(Computer):
    """Keep a teammate's guest across controller lifetimes."""

    def __init__(self, manifest: Path, owner: str) -> None:
        """Bind an owner-checked manifest before any VM operation."""
        super().__init__()
        self.manifest, self.owner = manifest, owner
        self._manual = False
        self._deleting = False
        if manifest.exists():
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if record.get("owner") != owner:
                raise ComputerError("Computer manifest belongs to another owner")
            if not re.fullmatch(r"nova-[0-9a-f]{16}", str(record.get("name", ""))):
                raise ComputerError("Invalid retained computer identity")
            self._name = record["name"]
            self._deleting = record.get("deleting") is True

    async def _create_guest(self, options: Any) -> Any:
        options.keep_on_failure = True
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        write_secret_json(self.manifest, {"owner": self.owner, "name": self._name})
        return await super()._create_guest(options)

    async def _cleanup_guest(self) -> None:
        """Startup failures must retain an existing computer and its recovery identity."""

    def _verify_owned(self, guest: Any) -> None:
        self._verify_info(guest.info())

    def _verify_info(self, info: Any) -> None:
        if (info.name, info.location, info.kind, info.runtime) != (
            self._name,
            "local",
            "vm",
            "qemu",
        ):
            raise ComputerError("Retained computer identity does not match its owner")

    async def _connect_owned(self) -> Any:
        _prepare_qemu_path()
        self._sdk = importlib.import_module("cua")
        self._client = self._sdk.embedded()
        info = await self._client.sandboxes().get("local:" + self._name)
        self._verify_info(info)
        # Cua 0.9.0 connect refuses Paused/Stopped sandboxes. Its supported
        # power API resumes/boots by ref before a fresh handle can attach.
        if getattr(info.status, "name", "") in {"SUSPENDED", "STOPPED"}:
            await self._client.spaces().start("local:" + self._name)
        guest = await self._client.sandboxes().connect("local:" + self._name)
        self._verify_owned(guest)
        return guest

    async def _start(self) -> dict:
        if self._deleting:
            raise ComputerError("Computer deletion is incomplete; retry Delete")
        if self._phase == "ready":
            return self._status()
        if self._starting is not None:
            raise ComputerError("Computer is already starting")
        if self._name is None:
            return await super()._start()
        self._phase = "starting"
        self._starting = asyncio.current_task()
        try:
            from novacode_cli.computer.windows_console import background_guest

            async with background_guest(self._name):
                self._guest = guest = await self._connect_owned()
                await guest.resume()
            self._desktop = await guest.spacesd(30_000)
            self._paused = False
            self._manual = False
            self._observation = None
            await self._capture(observe=False)
            self._phase, self._stage, self._error = "ready", "Retained desktop ready", ""
            return self._status()
        except BaseException as exc:
            self._phase, self._error = "error", str(exc)
            self._desktop = None
            raise
        finally:
            self._starting = None

    async def _stop(self) -> dict:
        self._paused = True
        self._observation = None
        if self._starting is not None:
            # Do not race create/suspend or delete a half-created persistent guest.
            raise ComputerError("Computer is still starting; wait before stopping")
        async with self._input_lock:
            if self._name and self._guest is None:
                self._sdk = importlib.import_module("cua")
                self._client = self._sdk.embedded()
                info = await self._client.sandboxes().get("local:" + self._name)
                self._verify_info(info)
                if getattr(info.status, "name", "") not in {"SUSPENDED", "STOPPED"}:
                    await self._client.spaces().stop("local:" + self._name)
            if self._guest is not None and self._phase != "suspended":
                try:
                    await self._guest.suspend()
                except BaseException as exc:
                    self._phase, self._error = "error", str(exc)
                    raise
            self._manual = False
            self._desktop = None
            self._phase = "suspended" if self._name else "stopped"
            self._stage = "Computer retained; resume to continue"
            return self._status()

    def _status(self) -> dict:
        return {
            **super()._status(),
            "persistent": True,
            "manual_control": self._manual,
            "manual": self._manual,
        }

    @property
    def manual(self) -> bool:
        """Whether the owner holds input independently of task state."""
        return self._manual

    async def _delete_owned(self) -> dict:
        if self._starting is not None:
            raise ComputerError("Computer is still starting")
        async with self._input_lock:
            self._paused = True
            if self._name:
                self._sdk = importlib.import_module("cua")
                self._client = self._sdk.embedded()
                try:
                    if self._guest is None:
                        info = await self._client.sandboxes().get("local:" + self._name)
                        self._verify_info(info)
                    else:
                        self._verify_owned(self._guest)
                    write_secret_json(
                        self.manifest,
                        {
                            "owner": self.owner,
                            "name": self._name,
                            "deleting": True,
                        },
                    )
                    self._deleting = True
                    if self._guest is None:
                        await self._client.sandboxes().delete("local:" + self._name)
                    else:
                        await self._guest.delete()
                except self._sdk.CuaError.NotFound:
                    if not self._deleting:
                        raise
                self._guest = None
                self._name = None
            self.manifest.unlink(missing_ok=True)
            self._desktop = self._observation = None
            self._manual = False
            self._frame = b""
            self._phase, self._stage = "stopped", "Computer deleted"
            return self._status()

    async def _dispatch(self, operation: str, arguments: dict) -> Any:
        operation = {
            "takeover": "take_control",
            "return_control": "release_control",
            "manual_action": "owner_action",
        }.get(operation, operation)
        if operation == "resume" and self._manual:
            raise ComputerError("Return Control explicitly before resuming agent input")
        if operation == "approval_frame":
            async with self._input_lock:
                shot = self._observation
                if shot is None or shot.screenshot_id != arguments.get("screenshot_id"):
                    raise ComputerError("Take a fresh screenshot before proposing input")
                return bytes(shot.image)
        if operation == "delete":
            return await self._delete_owned()
        if operation == "take_control":
            self._paused = True
            async with self._input_lock:
                self._manual = True
                self._observation = None
                return self._status()
        if operation == "release_control":
            async with self._input_lock:
                self._manual = False
                self._paused = False
                self._observation = None
                return self._status()
        if operation == "owner_action":
            async with self._input_lock:
                if not self._manual or self._desktop is None:
                    raise ComputerError("Take manual control before sending input")
                from novacode_cli.tools.computer_tools import ComputerActionInput

                await self._capture(observe=True)
                width = arguments.pop("frame_width", None)
                height = arguments.pop("frame_height", None)
                if width is not None and (width, height) != (
                    self._observation.width,
                    self._observation.height,
                ):
                    raise ComputerError("Display dimensions changed; wait for a fresh preview")
                validated = ComputerActionInput.model_validate(
                    {**arguments, "screenshot_id": self._observation.screenshot_id}
                )
                self._paused = False
                try:
                    await self._act(**validated.model_dump())
                    await self._capture(observe=False)
                finally:
                    self._paused = True
                    self._observation = None
                return self._status()
        return await super()._dispatch(operation, arguments)
