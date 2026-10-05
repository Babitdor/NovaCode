"""Standalone updater: no Nova imports, and no running Nova launcher to replace."""

from __future__ import annotations

# The helper streams installer output and its result to the inherited terminal.
# ruff: noqa: T201, S603
import ctypes
import json
import subprocess
import sys
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path

ERROR_INVALID_PARAMETER = 87
WAIT_FAILED = 0xFFFFFFFF


def wait_for_exit(pid: int, *, launcher_only: bool = False) -> None:
    """Wait for the caller, and for its parent only if that parent is nova.exe."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handle = kernel.OpenProcess(0x100000 | 0x1000, 0, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:  # Process already exited.
            return
        raise ctypes.WinError(error)
    try:
        if launcher_only:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if Path(buffer.value).name.lower() != "nova.exe":
                return  # Never wait for the user's PowerShell or terminal to close.
        result = kernel.WaitForSingleObject(handle, 60000)
        if result == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if result != 0:
            message = "Nova's update launcher did not exit within 60 seconds."
            raise TimeoutError(message)
    finally:
        kernel.CloseHandle(handle)


def run_installer(command: list[str]) -> bool:
    """Stream installer output and distinguish a successful no-op upgrade."""
    no_upgrade = False
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        if process.stdout is None:
            message = "Could not read updater output."
            raise OSError(message)
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.strip().casefold() == "nothing to upgrade":
                no_upgrade = True
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
    return not no_upgrade


def main(payload: dict) -> int:
    """Run uv only after both the Python caller and its executable wrapper exit."""
    try:
        print("Waiting for Nova's update launcher to exit…", flush=True)
        wait_for_exit(payload["caller_pid"])
        wait_for_exit(payload["parent_pid"], launcher_only=True)
        changed = run_installer(payload["command"])
        with suppress(OSError):
            Path(payload["cache"]).unlink(missing_ok=True)
        print(
            "Nova updated. Restart Nova to use the new code."
            if changed
            else "NovaCode is up to date.",
            flush=True,
        )
        return 0  # noqa: TRY300
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Update failed: {error}", flush=True)
        print("Close other Nova sessions and retry: uv tool upgrade novacode-cli --reinstall")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(json.loads(sys.argv[1])))
