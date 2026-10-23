"""Install Root's optional Node SDK outside the project, on explicit setup."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

ASSETS = Path(__file__).with_name("rootapp_bot")


def sdk_home() -> Path:
    from novacode_cli.remote.config import _CONFIG_DIR

    return _CONFIG_DIR / "rootapp"


def node_executable() -> str:
    executable = shutil.which("node")
    if not executable:
        raise RuntimeError(
            "Rootapp requires Node.js 22 or newer. Install Node.js, then restart Nova."
        )
    return executable


def devhost_entry() -> Path:
    return sdk_home() / "node_modules" / "@rootsdk" / "dev-tools" / "bin" / "rootsdk-devhost"


def subprocess_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Use Nova's existing process-tree cleanup for DevHost and npm children."""
    from novacode_cli.shell.middleware import ShellMiddleware

    await ShellMiddleware._terminate_tree(process, grace=2)
    await process.wait()


async def setup_rootapp() -> str:
    """Install the locked optional SDK. Never called just by opening /remote."""
    node = node_executable()
    check = await asyncio.create_subprocess_exec(
        node, "--version", stdout=asyncio.subprocess.PIPE, **subprocess_options()
    )
    try:
        output, _ = await asyncio.wait_for(check.communicate(), 10)
    finally:
        if check.returncode is None:
            await stop_process(check)
    if int(output.decode().strip().lstrip("v").split(".")[0]) < 22:
        raise RuntimeError("Rootapp requires Node.js 22 or newer.")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm is required to install Root's SDK.")
    home = sdk_home()
    await asyncio.to_thread(home.mkdir, parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        await asyncio.to_thread(shutil.copyfile, ASSETS / name, home / name)
    process = await asyncio.create_subprocess_exec(
        npm,
        "ci",
        "--no-audit",
        "--no-fund",
        cwd=home,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **subprocess_options(),
    )
    try:
        async with asyncio.timeout(180):
            code = await process.wait()
    finally:
        if process.returncode is None:
            await stop_process(process)
    if code:
        raise RuntimeError(
            "Root SDK installation failed. Check Node.js, npm and network access, then retry setup."
        )
    return (
        "Rootapp SDK installed. Configure your Dev Token, Bot ID and channel, then start Rootapp."
    )
