"""Version-pinned recovery for Cua 0.9.0's Windows QEMU interrupt-controller bug."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

WHPX_ARGS = ["-machine", "kernel-irqchip=off"]
MAX_STATE_BYTES = 1_000_000
MAX_PORT = 65535


def _saved_state(name: str) -> tuple[Path, dict[str, Any]]:
    """Read only the named, Nova-owned Linux guest's expected Cua state."""
    if not re.fullmatch(r"nova-[0-9a-f]{16}", name):
        message = "Refusing WHPX recovery for a guest not owned by Nova."
        raise ValueError(message)
    root = Path(os.environ.get("CUA_HOME", str(Path.home() / ".cua"))) / "vmm" / "qemu"
    directory = root / name
    path = directory / "state.json"
    if directory.resolve() != root.resolve() / name or path.is_symlink():
        message = "Refusing WHPX recovery through a redirected guest state path."
        raise ValueError(message)
    if path.stat().st_size > MAX_STATE_BYTES:
        message = "Cua guest state is unexpectedly large."
        raise ValueError(message)
    state = json.loads(path.read_text(encoding="utf-8"))
    expected = {"name": name, "kind": "instance", "arch": "x86_64", "os": "linux"}
    if not isinstance(state, dict) or any(
        state.get(key) != value for key, value in expected.items()
    ):
        message = "Cua guest state does not describe Nova's local Linux QEMU guest."
        raise ValueError(message)
    if state.get("extra_args") not in ([], WHPX_ARGS):
        message = "Refusing to replace unexpected existing QEMU launch arguments."
        raise ValueError(message)
    return path, state


def configure_whpx(name: str, expected_pid: int | None) -> None:
    """Atomically set one argument on the stopped guest, preserving all other state.

    Cua 0.9.0's creation API does not expose QEMU arguments. Its pinned runtime
    persists ``extra_args`` and preserves them when starting an existing guest.
    Refuse unexpected state or a live process rather than editing a running VM.
    """
    path, state = _saved_state(name)
    if state.get("pid") not in (None, expected_pid) or (
        psutil.pid_exists(expected_pid)
        if expected_pid is not None
        else (path.parent / "qemu.pid").exists()
    ):
        message = "WHPX recovery requires the original guest process to have stopped."
        raise ValueError(message)
    state["extra_args"] = WHPX_ARGS
    descriptor, temporary = tempfile.mkstemp(prefix=".nova-whpx-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _listener_pid(name: str, port: int) -> int | None:
    """Recover Cua's cleared PID only from this guest's verified QEMU listener."""
    for connection in psutil.net_connections(kind="tcp"):
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.laddr.ip == "127.0.0.1"
            and connection.laddr.port == port
            and connection.pid is not None
        ):
            process = psutil.Process(connection.pid)
            arguments = process.cmdline()
            binary = Path(process.exe()).name.casefold()
            if (
                binary in {"qemu-system-x86_64", "qemu-system-x86_64.exe"}
                and "-name" in arguments
                and arguments[arguments.index("-name") + 1 :][:1] == [name]
            ):
                return connection.pid
            message = "The saved management port is not owned by Nova's QEMU process."
            raise ValueError(message)
    return None


async def recover_whpx(name: str) -> None:
    """Verify the paused VM over loopback QMP, stop it, then update its launch state."""
    _, state = _saved_state(name)
    pid, port = state.get("pid"), state.get("qmp_port")
    if (
        (pid is not None and (type(pid) is not int or pid <= 0))
        or type(port) is not int
        or not 0 < port <= MAX_PORT
    ):
        message = "WHPX recovery requires a valid guest process and loopback management port."
        raise ValueError(message)
    if pid is None:
        pid = await asyncio.to_thread(_listener_pid, name, port)
    if pid is None:
        # Cua's launch guard normally already stopped a failed QEMU. Verify
        # that its management listener is gone before modifying the saved state.
        async with asyncio.timeout(5):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except ConnectionRefusedError:
                configure_whpx(name, None)
                return
            writer.close()
            await writer.wait_closed()
        message = "Refusing WHPX recovery while an untracked guest listener is still running."
        raise ValueError(message)
    process = psutil.Process(pid)
    async with asyncio.timeout(10):
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=65536)
        try:
            greeting = json.loads(await reader.readline())
            if "QMP" not in greeting:
                message = "The guest management endpoint did not identify itself as QMP."
                raise ValueError(message)

            async def command(operation: str) -> dict[str, Any]:
                writer.write((json.dumps({"execute": operation}) + "\n").encode())
                await writer.drain()
                while True:
                    reply = json.loads(await reader.readline())
                    if "error" in reply:
                        message = f"Guest management rejected {operation}."
                        raise ValueError(message)
                    if "return" in reply:
                        return reply["return"]

            await command("qmp_capabilities")
            identity = await command("query-name")
            status = await command("query-status")
            if identity.get("name") != name or status.get("status") != "paused":
                message = "Refusing WHPX recovery for a different or non-paused guest."
                raise ValueError(message)
            await command("quit")
        finally:
            writer.close()
            # QEMU can close its management socket as it exits after quit.
            with suppress(OSError):
                await writer.wait_closed()
    await asyncio.to_thread(process.wait, timeout=10)
    configure_whpx(name, pid)
