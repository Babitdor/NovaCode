"""Standalone updater: no Nova imports, and no running Nova launcher to replace."""

from __future__ import annotations

import ast

# The helper streams installer output and its result to the inherited terminal.
# ruff: noqa: T201, S603
import ctypes
import json
import shutil
import subprocess
import sys
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path

ERROR_INVALID_PARAMETER = 87
WAIT_FAILED = 0xFFFFFFFF


def installed_version(python: str | None = None) -> str:
    """Read the updated environment, rather than an already-imported version."""
    if python:
        try:
            return subprocess.check_output(
                [python, "-I", "-c",
                 "from importlib.metadata import version; print(version('novacode-cli'))"],
                text=True, encoding="utf-8", stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        tree = ast.parse(Path(__file__).with_name("_version.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            ):
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    return value
    except (OSError, ValueError, SyntaxError):
        pass
    return "unavailable"


def show_update_success(version: str, logo: str = "NOVA") -> None:
    """Replace interactive progress with a compact, centered completion screen."""
    interactive = sys.stdout.isatty()
    if interactive and sys.platform == "win32":
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.restype = wintypes.HANDLE
            handle = kernel.GetStdHandle(-11)
            mode = wintypes.DWORD()
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            interactive = bool(
                kernel.GetConsoleMode(handle, ctypes.byref(mode))
                and kernel.SetConsoleMode(handle, mode.value | 0x0004)
            )
        except (OSError, AttributeError):
            interactive = False
    if interactive:
        print("\x1b[2J\x1b[H", end="")
    width = min(shutil.get_terminal_size((80, 24)).columns, 100)
    rows = logo.splitlines()
    if any(len(row) > width for row in rows):
        rows = ["NOVA"]
    print()
    for row in [*rows, "", f"v{version}", "", "Update successful.", "", "Restart Nova", ""]:
        safe = row.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"
        )
        text = safe.center(width).rstrip() if interactive else safe.rstrip()
        if interactive and row in rows:
            text = f"\x1b[38;2;122;162;247m{text}\x1b[0m"
        elif interactive and row == "Update successful.":
            text = f"\x1b[32;1m{text}\x1b[0m"
        print(text, flush=True)


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
        if changed:
            show_update_success(
                installed_version(payload.get("version_python")), payload.get("logo", "NOVA")
            )
        else:
            print("NovaCode is up to date.", flush=True)
        return 0  # noqa: TRY300
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Update failed: {error}", flush=True)
        print("Close other Nova sessions and retry: uv tool upgrade novacode-cli --reinstall")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(json.loads(sys.argv[1])))
