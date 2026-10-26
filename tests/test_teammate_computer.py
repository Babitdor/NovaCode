"""Persistent guests cannot be confused with temporary session computers."""
# ruff: noqa: F811

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.computer.persistent import PersistentComputer
from novacode_cli.computer.runtime import ComputerError
from tests.test_computer import sdk  # noqa: F401 -- SDK boundary fixture.

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def retained_sdk(sdk: SimpleNamespace):
    class NotFoundError(Exception):
        pass

    sdk.CuaError = SimpleNamespace(NotFound=NotFoundError)

    async def get(_ref: str) -> SimpleNamespace:
        info = sdk.guest.info()
        info.status = SimpleNamespace(name="RUNNING")
        return info

    sdk.boxes.get = AsyncMock(side_effect=get)


async def test_stop_retains_guest_and_restart_reattaches(tmp_path: Path, sdk: SimpleNamespace):
    manifest = tmp_path / "computer.json"
    computer = PersistentComputer(manifest, "owner-one")
    sdk.guest.suspend = AsyncMock()
    sdk.guest.resume = AsyncMock()
    await computer.call("start")
    name = json.loads(manifest.read_text())["name"]
    sdk.guest.info = lambda: SimpleNamespace(name=name, location="local", kind="vm", runtime="qemu")
    sdk.boxes.connect = AsyncMock(return_value=sdk.guest)
    await computer.call("stop")
    sdk.guest.suspend.assert_awaited_once()
    sdk.guest.delete.assert_not_awaited()
    assert manifest.exists()
    assert computer.close()
    restored = PersistentComputer(manifest, "owner-one")
    try:
        await restored.call("start")
        sdk.boxes.connect.assert_awaited_with("local:" + name)
        sdk.guest.resume.assert_awaited_once()
        assert (await restored.call("status"))["phase"] == "ready"
        await restored.call("delete")
        sdk.guest.delete.assert_awaited_once()
        assert not manifest.exists()
    finally:
        restored.close()


def test_foreign_manifest_is_rejected(tmp_path: Path):
    manifest = tmp_path / "computer.json"
    manifest.write_text(json.dumps({"owner": "other", "name": "nova-0123456789abcdef"}))
    with pytest.raises(ComputerError, match="owner"):
        PersistentComputer(manifest, "mine")


async def test_suspend_failure_retains_guest_and_ownership(tmp_path: Path, sdk: SimpleNamespace):
    manifest = tmp_path / "computer.json"
    computer = PersistentComputer(manifest, "mine")
    sdk.guest.suspend = AsyncMock(side_effect=RuntimeError("suspend failed"))
    await computer.call("start")
    name = json.loads(manifest.read_text())["name"]
    sdk.guest.info = lambda: SimpleNamespace(name=name, location="local", kind="vm", runtime="qemu")
    with pytest.raises(RuntimeError, match="suspend failed"):
        await computer.call("stop")
    assert manifest.exists()
    sdk.guest.delete.assert_not_awaited()
    sdk.guest.suspend.side_effect = None
    await computer.call("delete")
    computer.close()


async def test_cold_suspended_guest_uses_power_api_without_replacement(
    tmp_path: Path, sdk: SimpleNamespace
):
    manifest = tmp_path / "computer.json"
    name = "nova-0123456789abcdef"
    manifest.write_text(json.dumps({"owner": "mine", "name": name}))
    sdk.guest.info = lambda: SimpleNamespace(name=name, location="local", kind="vm", runtime="qemu")
    info = sdk.guest.info()
    info.status = SimpleNamespace(name="SUSPENDED")
    sdk.boxes.get = AsyncMock(return_value=info)
    sdk.boxes.connect = AsyncMock(return_value=sdk.guest)
    sdk.guest.resume = AsyncMock()
    sdk.guest.suspend = AsyncMock()
    powers = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    sdk.embedded().spaces = lambda: powers
    computer = PersistentComputer(manifest, "mine")
    try:
        await computer.call("stop")
        sdk.boxes.connect.assert_not_awaited()
        powers.start.assert_not_awaited()
        await computer.call("start")
        powers.start.assert_awaited_once_with("local:" + name)
        sdk.boxes.create.assert_not_awaited()
        sdk.guest.delete.assert_not_awaited()
    finally:
        computer.close()


async def test_manual_control_gates_agent_scales_input_and_keeps_text_private(
    tmp_path: Path, sdk: SimpleNamespace
):
    computer = PersistentComputer(tmp_path / "computer.json", "mine")
    sdk.guest.suspend = AsyncMock()
    try:
        await computer.call("start")
        await computer.call("screenshot")
        await computer.call("takeover")
        with pytest.raises(ComputerError):
            await computer.call("action", action="click", screenshot_id="old", x=100, y=100)
        await computer.call(
            "manual_action", action="click", x=200, y=100, frame_width=800, frame_height=600
        )
        sdk.desktop.click.assert_awaited_once_with(100, 50)
        with pytest.raises(ComputerError, match="dimensions"):
            await computer.call(
                "manual_action", action="click", x=200, y=100, frame_width=1600, frame_height=1200
            )
        await computer.call("manual_action", action="type", text="private-password")
        assert "private-password" not in str(await computer.call("status"))
        assert computer._observation is None
        await computer.call("return_control")
        with pytest.raises(ComputerError, match="fresh"):
            await computer.call("action", action="click", screenshot_id="old", x=100, y=100)
    finally:
        computer.close()


async def test_delete_recovers_crash_after_guest_deleted_before_manifest_removed(
    tmp_path: Path, sdk: SimpleNamespace
):
    manifest = tmp_path / "computer.json"
    manifest.write_text(
        json.dumps(
            {
                "owner": "mine",
                "name": "nova-0123456789abcdef",
                "deleting": True,
            }
        )
    )
    sdk.boxes.get.side_effect = sdk.CuaError.NotFound()
    computer = PersistentComputer(manifest, "mine")
    try:
        with pytest.raises(ComputerError, match="deletion is incomplete"):
            await computer.call("start")
        await computer.call("delete")
        assert not manifest.exists()
        sdk.boxes.create.assert_not_awaited()
        sdk.boxes.delete.assert_not_awaited()
    finally:
        computer.close()
