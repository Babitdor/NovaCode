"""Windows WHPX recovery is bounded to the pinned SDK and Nova's own paused guest."""

# ruff: noqa: F811 - Imported pytest fixtures are named in test parameters.

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.computer import runtime
from novacode_cli.computer.windows_qemu import WHPX_ARGS, configure_whpx, recover_whpx
from tests.test_computer import sdk  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

NAME = "nova-0123456789abcdef"


def saved_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a fixture matching the pinned Cua runtime's saved guest schema."""
    monkeypatch.setenv("CUA_HOME", str(tmp_path))
    directory = tmp_path / "vmm" / "qemu" / NAME
    directory.mkdir(parents=True)
    path = directory / "state.json"
    path.write_text(
        json.dumps(
            {
                "name": NAME,
                "kind": "instance",
                "arch": "x86_64",
                "os": "linux",
                "paused": False,
                "pid": 12345,
                "qmp_port": 4444,
                "extra_args": [],
                "disk": "keep-original-disk",
                "private": "keep-private",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("novacode_cli.computer.windows_qemu.psutil.pid_exists", lambda _pid: False)
    return path


def test_workaround_preserves_every_other_saved_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = saved_state(tmp_path, monkeypatch)
    before = json.loads(path.read_text())
    configure_whpx(NAME, 12345)
    after = json.loads(path.read_text())
    assert after == {**before, "extra_args": WHPX_ARGS}
    configure_whpx(NAME, 12345)
    assert json.loads(path.read_text()) == after
    assert not list(path.parent.glob(".nova-whpx-*"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", 999),
        ("name", "someone-else"),
        ("kind", "checkpoint"),
        ("arch", "aarch64"),
        ("os", "windows"),
        ("extra_args", ["-netdev", "custom"]),
    ],
)
def test_unexpected_guest_metadata_is_never_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
):
    path = saved_state(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"guest|QEMU"):
        configure_whpx(NAME, 12345)
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["other-vm", "../nova-0123456789abcdef", "nova-short"])
def test_unowned_names_are_rejected(name: str):
    with pytest.raises(ValueError, match="not owned by Nova"):
        configure_whpx(name, 12345)


async def test_windows_recovers_once_then_observes_desktop(
    sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sys, "platform", "win32")
    sdk.__version__ = "0.9.0"
    sdk.boxes.create.side_effect = [
        RuntimeError("qemu started but VM status is 'paused'"),
        sdk.guest,
    ]
    computer = runtime.get_computer("whpx-recovery")
    monkeypatch.setattr(computer, "_startup_error", lambda _exc: "WHPX boot failure")
    recover = AsyncMock()
    monkeypatch.setattr("novacode_cli.computer.windows_qemu.recover_whpx", recover)
    assert (await computer.call("start"))["phase"] == "ready"
    name = sdk.boxes.create.call_args.args[0].name
    assert sdk.boxes.create.call_args.args[0].keep_on_failure
    recover.assert_awaited_once_with(name)
    assert sdk.boxes.create.await_count == 2
    sdk.desktop.screenshot.assert_awaited_once()
    await computer.call("stop")
    sdk.guest.delete.assert_awaited_once()


@pytest.mark.parametrize(
    ("platform", "version", "diagnostic"),
    [
        ("linux", "0.9.0", "WHPX"),
        ("win32", "0.10.0", "WHPX"),
        ("win32", "0.9.0", "different failure"),
    ],
)
async def test_workaround_never_retries_other_platforms_versions_or_errors(
    sdk: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    version: str,
    diagnostic: str,
):
    monkeypatch.setattr(sys, "platform", platform)
    sdk.__version__ = version
    sdk.boxes.create.side_effect = RuntimeError("qemu started but VM status is 'paused'")
    sdk.boxes.connect = AsyncMock()
    computer = runtime.get_computer("no-whpx-retry")
    monkeypatch.setattr(computer, "_startup_error", lambda _exc: diagnostic)
    with pytest.raises(runtime.ComputerError):
        await computer.call("start")
    sdk.boxes.connect.assert_not_awaited()


async def test_launch_guard_already_stopped_guest_is_reconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = saved_state(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data["pid"] = None
    path.write_text(json.dumps(data))
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=ConnectionRefusedError))
    monkeypatch.setattr("novacode_cli.computer.windows_qemu._listener_pid", lambda _n, _p: None)
    await recover_whpx(NAME)
    assert json.loads(path.read_text()) == {**data, "extra_args": WHPX_ARGS}


@pytest.mark.parametrize(
    ("identity", "status", "metadata_pid"),
    [
        (NAME, "paused", 12345),
        (NAME, "paused", None),
        ("someone-else", "paused", 12345),
        (NAME, "running", 12345),
    ],
)
async def test_qmp_recovery_requires_matching_paused_guest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    status: str,
    metadata_pid: int | None,
):
    path = saved_state(tmp_path, monkeypatch)
    operations = []

    async def management(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"QMP":{}}\n')
        await writer.drain()
        try:
            while line := await reader.readline():
                operation = json.loads(line)["execute"]
                operations.append(operation)
                result = {"query-name": {"name": identity}, "query-status": {"status": status}}
                writer.write((json.dumps({"event": "STATUS"}) + "\n").encode())
                writer.write((json.dumps({"return": result.get(operation, {})}) + "\n").encode())
                await writer.drain()
                if operation == "quit":
                    break
        finally:
            writer.close()
            await writer.wait_closed()

    process = SimpleNamespace(wait=Mock(return_value=0))
    monkeypatch.setattr("novacode_cli.computer.windows_qemu.psutil.Process", lambda _pid: process)
    monkeypatch.setattr("novacode_cli.computer.windows_qemu._listener_pid", lambda _n, _p: 12345)
    async with await asyncio.start_server(management, "127.0.0.1", 0) as server:
        data = json.loads(path.read_text())
        data["pid"] = metadata_pid
        data["qmp_port"] = server.sockets[0].getsockname()[1]
        path.write_text(json.dumps(data))
        if identity == NAME and status == "paused":
            await recover_whpx(NAME)
            assert operations[-1] == "quit"
            process.wait.assert_called_once_with(timeout=10)
            assert json.loads(path.read_text())["extra_args"] == WHPX_ARGS
        else:
            with pytest.raises(ValueError, match="different or non-paused"):
                await recover_whpx(NAME)
            assert "quit" not in operations
            process.wait.assert_not_called()
            assert json.loads(path.read_text()) == data


def test_live_process_prevents_saved_state_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = saved_state(tmp_path, monkeypatch)
    before = path.read_bytes()
    monkeypatch.setattr("novacode_cli.computer.windows_qemu.psutil.pid_exists", lambda _pid: True)
    with pytest.raises(ValueError, match="process to have stopped"):
        configure_whpx(NAME, 12345)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("binary", "name", "allowed"),
    [
        ("qemu-system-x86_64.exe", NAME, True),
        ("qemu-system-x86_64.exe", "other-guest", False),
        ("python.exe", NAME, False),
    ],
)
def test_cleared_pid_lookup_checks_the_listener_process(
    monkeypatch: pytest.MonkeyPatch, binary: str, name: str, *, allowed: bool
):
    from novacode_cli.computer import windows_qemu

    connection = SimpleNamespace(
        status=windows_qemu.psutil.CONN_LISTEN,
        laddr=SimpleNamespace(ip="127.0.0.1", port=4444),
        pid=12345,
    )
    process = SimpleNamespace(cmdline=lambda: ["qemu", "-name", name], exe=lambda: "/bin/" + binary)
    monkeypatch.setattr(windows_qemu.psutil, "net_connections", lambda **_kw: [connection])
    monkeypatch.setattr(windows_qemu.psutil, "Process", lambda _pid: process)
    if allowed:
        assert windows_qemu._listener_pid(NAME, 4444) == 12345
    else:
        with pytest.raises(ValueError, match="not owned by Nova"):
            windows_qemu._listener_pid(NAME, 4444)


async def test_user_stop_prevents_automatic_recovery(
    sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sys, "platform", "win32")
    sdk.__version__ = "0.9.0"
    sdk.boxes.create.side_effect = RuntimeError("qemu started but VM status is 'paused'")
    computer = runtime.Computer()
    computer._sdk, computer._client, computer._name = sdk, sdk.embedded(), NAME
    computer._phase = "stopping"
    recover = AsyncMock()
    monkeypatch.setattr(computer, "_startup_error", lambda _exc: "WHPX boot failure")
    monkeypatch.setattr("novacode_cli.computer.windows_qemu.recover_whpx", recover)
    with pytest.raises(RuntimeError, match="paused"):
        await computer._create_guest(SimpleNamespace())
    recover.assert_not_awaited()


def test_later_errors_are_not_mislabeled_as_the_original_whpx_failure():
    computer = runtime.Computer()
    computer._whpx_recovery_attempted = True
    error = computer._startup_error(RuntimeError("desktop connection failed"))
    assert error == "Windows WHPX compatibility recovery failed: desktop connection failed"


def windows_disconnect(code: int = 64) -> OSError:
    """Build the same disconnect on Windows and Linux test runners."""
    error = OSError("The specified network name is no longer available")
    error.winerror = code
    return error


@pytest.mark.parametrize(
    "failure",
    [
        "read",
        "drain",
        "eof",
        "handshake",
        "connect",
        "greeting_eof",
        "permission",
        "alive",
        "handshake_alive",
    ],
)
async def test_qmp_disconnect_requires_authorized_quit_and_confirmed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    from novacode_cli.computer import windows_qemu

    path = saved_state(tmp_path, monkeypatch)
    before = path.read_bytes()
    replies = [
        b'{"QMP":{}}\n',
        b'{"return":{}}\n',
        (json.dumps({"return": {"name": NAME}}) + "\n").encode(),
        b'{"return":{"status":"paused"}}\n',
    ]
    early_disconnect = failure in {"handshake", "connect", "greeting_eof", "handshake_alive"}
    if failure in {"handshake", "handshake_alive"}:
        replies = [windows_disconnect()]
    elif failure == "greeting_eof":
        replies = [b""]
    else:
        replies.append(
            b"" if failure == "eof" else windows_disconnect(5 if failure == "permission" else 64)
        )
    reader = SimpleNamespace(readline=AsyncMock(side_effect=replies))
    writer = SimpleNamespace(
        write=Mock(),
        drain=AsyncMock(),
        close=Mock(),
        wait_closed=AsyncMock(side_effect=windows_disconnect()),
    )
    if failure == "drain":
        writer.drain.side_effect = [None, None, None, windows_disconnect()]
    process = SimpleNamespace(wait=Mock(return_value=0))
    if failure in {"alive", "handshake_alive"}:
        process.wait.side_effect = windows_qemu.psutil.TimeoutExpired(10, pid=12345)
    connect = AsyncMock(return_value=(reader, writer))
    if failure == "connect":
        connect.side_effect = windows_disconnect()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(windows_qemu.psutil, "Process", lambda _pid: process)
    closed = AsyncMock()
    monkeypatch.setattr(windows_qemu, "_wait_listener_closed", closed)
    if failure in {"permission", "alive", "handshake_alive"}:
        expected = windows_qemu.psutil.TimeoutExpired if failure != "permission" else OSError
        with pytest.raises(expected):
            await recover_whpx(NAME)
        assert path.read_bytes() == before
        if failure == "permission":
            process.wait.assert_not_called()
    else:
        await recover_whpx(NAME)
        process.wait.assert_called_once_with(timeout=10)
        assert json.loads(path.read_text())["extra_args"] == WHPX_ARGS
        assert closed.await_count == int(early_disconnect)
    commands = [json.loads(call.args[0])["execute"] for call in writer.write.call_args_list]
    assert ("quit" in commands) == (not early_disconnect)


@pytest.mark.parametrize("failure", ["open", "close"])
async def test_cleared_pid_socket_shutdown_is_rechecked_before_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    from novacode_cli.computer import windows_qemu

    path = saved_state(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data["pid"] = None
    path.write_text(json.dumps(data))
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock(side_effect=windows_disconnect()))
    first = windows_disconnect() if failure == "open" else (None, writer)
    connect = AsyncMock(side_effect=[first, ConnectionRefusedError()])
    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(windows_qemu, "_listener_pid", lambda _n, _p: None)
    await recover_whpx(NAME)
    assert connect.await_count == 2
    assert json.loads(path.read_text()) == {**data, "extra_args": WHPX_ARGS}
