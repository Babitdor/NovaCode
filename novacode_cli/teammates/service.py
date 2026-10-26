"""Authenticated loopback control and lazy detached worker launch."""
# ruff: noqa: ANN401, EM101, TRY003, T201

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import psutil
from aiohttp import web

from novacode_cli.security.secret_files import write_secret_json
from novacode_cli.teammates.controller import TeammateController
from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from pathlib import Path

MAX_PORT = 65536
HTTP_OK = 200
MIN_TOKEN_LENGTH = 32


def profile_directory() -> Path:
    """Resolve the same profile root regardless of project or tab."""
    from novacode_cli.config.config import settings

    return settings.user_deepagents_dir / "teammates"


def endpoint(directory: Path) -> dict | None:
    """Reject stale discovery files and reused process IDs."""
    try:
        value = json.loads((directory / "service.json").read_text(encoding="utf-8"))
        if psutil.Process(value["pid"]).create_time() != value["born"]:
            return None
        if type(value["port"]) is not int or not 0 < value["port"] < MAX_PORT:
            return None
        if not isinstance(value.get("token"), str) or len(value["token"]) < MIN_TOKEN_LENGTH:
            return None
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        return None
    else:
        return value


async def request(action: str, payload: dict | None = None, *, launch: bool = True) -> dict:
    """Reconnect, or lazily launch the fixed worker module without a shell."""
    directory = profile_directory()
    current = await asyncio.to_thread(endpoint, directory)
    if current is None and launch:
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "cwd": str(directory),
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            options["start_new_session"] = True
        # Concurrent launchers may spawn candidates. SQLite ownership allows
        # exactly one to publish a listener or run work.
        await asyncio.to_thread(
            subprocess.Popen, [sys.executable, "-m", "novacode_cli.teammates.worker"], **options
        )
        deadline = time.monotonic() + 15
        while current is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            current = await asyncio.to_thread(endpoint, directory)
    if current is None:
        if launch:
            raise TeamError(
                "Teammate worker did not start; inspect profile ownership and installation"
            )
        return {"worker": "stopped"}
    timeout = aiohttp.ClientTimeout(total=660 if action == "takeover" else 90)
    async with (
        aiohttp.ClientSession(timeout=timeout, trust_env=False) as client,
        client.post(
            f"http://127.0.0.1:{current['port']}/control/{action}",
            json=payload or {},
            headers={"X-Nova-Token": current["token"], "X-Nova-Request": "1"},
            allow_redirects=False,
        ) as response,
    ):
        result = await response.json()
        if response.status != HTTP_OK:
            raise TeamError(result.get("error", "Teammate service unavailable"))
        return result


class PreviewOwner:
    """Route preview control through durable invalidation on the worker loop."""

    def __init__(self, controller: TeammateController) -> None:
        """Bind browser requests to the owning controller loop."""
        self.controller = controller
        self.loop = asyncio.get_running_loop()

    def call_sync(self, operation: str, **arguments: Any) -> Any:
        """Bridge preview handler threads without exposing service credentials."""
        return asyncio.run_coroutine_threadsafe(self.call(operation, arguments), self.loop).result(
            660
        )

    async def call(self, operation: str, arguments: dict) -> Any:
        """Allow only observation and explicit owner controls."""
        if self.controller.closed:
            raise TeamError("Teammate controller has closed")
        if operation in {"takeover", "return_control", "pause", "stop"}:
            await self.controller.command(operation, {})
            return await self.controller.computer.call("status")
        if operation == "resume":
            raise TeamError("Return Control, then Resume from Nova")
        if operation == "manual_action":
            if self.controller.data.get("deleting"):
                raise TeamError("Computer deletion is in progress")
            from novacode_cli.tools.computer_tools import ComputerActionInput

            arguments = dict(arguments)
            dimensions = {}
            if arguments.get("action") in {"click", "double_click", "right_click", "move", "drag"}:
                for key in ("frame_width", "frame_height"):
                    value = arguments.pop(key, None)
                    if type(value) is not int or value <= 0:
                        raise TeamError("Pointer input requires current image dimensions")
                    dimensions[key] = value
            validated = ComputerActionInput.model_validate(
                {**arguments, "screenshot_id": "manual"}
            ).model_dump()
            validated.pop("screenshot_id")
            return await self.controller.computer.call(operation, **validated, **dimensions)
        if operation == "approval_frame":
            approval = next(
                (a for a in self.controller.data.get("approvals", []) if a["state"] == "pending"),
                None,
            )
            if approval is None:
                raise TeamError("No pending observation")
            # Derive the path from a generated ID, never from HTTP/tool arguments.
            path = self.controller.directory / "observations" / f"{approval['id']}.png"
            return await asyncio.to_thread(path.read_bytes)
        if operation not in {"status", "frame"}:
            raise TeamError("Unknown preview operation")
        result = await self.controller.computer.call(operation)
        if operation == "status":
            result["pending_approval"] = any(
                a["state"] == "pending" for a in self.controller.data.get("approvals", [])
            )
        return result


async def serve(directory: Path) -> None:
    """Hold ownership until execution, VM shutdown and listener cleanup finish."""
    from novacode_cli.computer.preview import ComputerPreview

    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    controller = TeammateController(directory)
    await controller.open()
    token = secrets.token_urlsafe(32)
    shutdown = asyncio.Event()
    previews: list = []

    @web.middleware
    async def authorize(incoming: web.Request, handler: Any) -> web.Response:
        host = incoming.host
        expected_host = f"127.0.0.1:{incoming.transport.get_extra_info('sockname')[1]}"
        if (
            host != expected_host
            or (incoming.headers.get("Origin") not in {None, f"http://{host}"})
            or incoming.headers.get("X-Nova-Request") != "1"
            or not secrets.compare_digest(
                incoming.headers.get("X-Nova-Token", "").encode("utf-8"), token.encode("utf-8")
            )
        ):
            raise web.HTTPForbidden
        try:
            return await handler(incoming)
        except (ValueError, RuntimeError, OSError) as exc:
            return web.json_response({"error": str(exc)}, status=409)

    async def control(incoming: web.Request) -> web.Response:
        action = incoming.match_info["action"]
        payload = await incoming.json()
        if not isinstance(payload, dict):
            raise TeamError("Expected an object")
        if action == "shutdown":
            if controller.data:
                await controller.command("stop", {})
            shutdown.set()
            return web.json_response({"worker": "stopped"})
        if action == "preview":
            if not controller.computer:
                raise TeamError("Create a teammate first")
            if not previews:
                previews.append(ComputerPreview(PreviewOwner(controller)))
            return web.json_response({"url": previews[0].url})
        return web.json_response(await controller.command(action, payload))

    app = web.Application(middlewares=[authorize], client_max_size=128 * 1024)
    app.router.add_post("/control/{action}", control)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        write_secret_json(
            directory / "service.json",
            {
                "pid": os.getpid(),
                "born": psutil.Process().create_time(),
                "port": port,
                "token": token,
            },
        )
        await shutdown.wait()
    finally:
        # Fence survives until the retained guest is safely suspended.
        (directory / "service.json").unlink(missing_ok=True)
        await controller.close()
        for preview in previews:
            await asyncio.to_thread(preview.close)
        await runner.cleanup()


def cli(arguments: list[str]) -> int:
    """Inspect or gracefully stop the worker without importing the TUI."""
    if arguments not in (["status"], ["stop"]):
        print("Usage: nova teammates status|stop")
        return 2
    action = "status" if arguments == ["status"] else "shutdown"
    try:
        print(json.dumps(asyncio.run(request(action, launch=False)), indent=2))
    except (OSError, ValueError, aiohttp.ClientError) as exc:
        print(f"Teammates: {exc}")
        return 1
    return 0
