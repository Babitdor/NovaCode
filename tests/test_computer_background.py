"""Only a Nova VM's dedicated Windows console may be hidden."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.computer import windows_console

NAME = "nova-0123456789abcdef"


@pytest.mark.parametrize("members", [[123, 456], [123, 456, 789]])
def test_console_hide_refuses_shared_terminal(members: list[int], monkeypatch: pytest.MonkeyPatch):
    api = SimpleNamespace(
        attach=Mock(return_value=True),
        detach=Mock(),
        members=Mock(return_value=members),
        window=Mock(return_value=42),
        hide=Mock(),
    )
    monkeypatch.setattr(windows_console.os, "getpid", lambda: 456)
    assert windows_console._hide_console(123, api) == (len(members) == 2)
    api.attach.assert_called_once_with(123)
    api.detach.assert_called_once_with()
    if len(members) == 2:
        api.hide.assert_called_once_with(42)
    else:
        api.hide.assert_not_called()


def test_unattached_console_is_not_touched():
    api = SimpleNamespace(attach=Mock(return_value=False), detach=Mock(), hide=Mock())
    assert not windows_console._hide_console(123, api)
    api.hide.assert_not_called()
    api.detach.assert_not_called()


@pytest.mark.parametrize(
    ("binary", "name"), [("python.exe", NAME), ("qemu-system-x86_64.exe", "other")]
)
def test_process_identity_is_required(binary: str, name: str, monkeypatch: pytest.MonkeyPatch):
    process = SimpleNamespace(exe=lambda: "/bin/" + binary, cmdline=lambda: [binary, "-name", name])
    monkeypatch.setattr(windows_console.psutil, "Process", lambda _pid: process)
    assert not windows_console._owned_process(123, NAME)


async def test_background_monitor_covers_guest_and_stops(monkeypatch: pytest.MonkeyPatch):
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def monitor(name: str, stop: asyncio.Event, _seen: set[int]) -> None:
        assert name == NAME
        entered.set()
        try:
            await stop.wait()
        finally:
            finished.set()

    monkeypatch.setattr(windows_console.sys, "platform", "win32")
    monkeypatch.setattr(windows_console, "_monitor", monitor)
    async with windows_console.background_guest(NAME):
        await entered.wait()
        assert not finished.is_set()
    assert finished.is_set()


async def test_non_windows_creates_no_monitor(monkeypatch: pytest.MonkeyPatch):
    monitor = AsyncMock()
    monkeypatch.setattr(windows_console.sys, "platform", "linux")
    monkeypatch.setattr(windows_console, "_monitor", monitor)
    async with windows_console.background_guest(NAME):
        pass
    monitor.assert_not_called()


def test_helper_is_windowless_and_runs_once_per_pid(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.computer import windows_qemu

    monkeypatch.setattr(windows_qemu, "_saved_state", lambda _name: (None, {"pid": 123}))
    monkeypatch.setattr(
        windows_console, "_owned_process", lambda pid, name: pid == 123 and name == NAME
    )
    monkeypatch.setattr(windows_console.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    run = Mock()
    monkeypatch.setattr(windows_console.subprocess, "run", run)
    seen = set()
    windows_console._attempt(NAME, seen)
    windows_console._attempt(NAME, seen)
    run.assert_called_once()
    assert run.call_args.kwargs["creationflags"] == 0x08000000
    assert run.call_args.args[0][-2:] == ["123", NAME]
    assert run.call_args.kwargs["stdin"] == windows_console.subprocess.DEVNULL


async def test_monitor_finishes_after_startup_cancellation(monkeypatch: pytest.MonkeyPatch):
    entered, finished = asyncio.Event(), asyncio.Event()

    async def monitor(_name: str, stop: asyncio.Event, _seen: set[int]) -> None:
        entered.set()
        await stop.wait()
        finished.set()

    async def startup() -> None:
        async with windows_console.background_guest(NAME):
            await asyncio.Event().wait()

    monkeypatch.setattr(windows_console.sys, "platform", "win32")
    monkeypatch.setattr(windows_console, "_monitor", monitor)
    task = asyncio.create_task(startup())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_helper_detaches_its_inherited_console_before_attaching(monkeypatch: pytest.MonkeyPatch):
    api = SimpleNamespace(detach=Mock())

    def hide(pid: int, actual_api: object) -> bool:
        api.detach.assert_called_once_with()
        assert actual_api is api
        assert pid == 123
        return True

    monkeypatch.setattr(windows_console.sys, "platform", "win32")
    monkeypatch.setattr(windows_console.sys, "argv", ["helper", "123", NAME])
    monkeypatch.setattr(windows_console, "_owned_process", lambda *_args: True)
    monkeypatch.setattr(windows_console, "_ConsoleAPI", lambda: api)
    monkeypatch.setattr(windows_console, "_hide_console", hide)
    assert windows_console.main() == 0
