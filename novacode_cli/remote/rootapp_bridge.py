"""Root SDK DevHost companion connected over a private loopback WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import shutil
import socket
import tempfile
from collections import OrderedDict
from pathlib import Path

from aiohttp import WSMsgType, web

from novacode_cli.remote.bridge import BridgeConfig, RemoteMessage, RemotePlatform, chunk_message
from novacode_cli.remote.rootapp_setup import (
    ASSETS,
    devhost_entry,
    node_executable,
    sdk_home,
    stop_process,
    subprocess_options,
)
from novacode_cli.remote.streaming import PagedEditor, connected_editor


class RootappBridge:
    def __init__(self, config: BridgeConfig, message_queue: asyncio.Queue, *, on_status=None):
        self._config, self._queue, self._on_status = config, message_queue, on_status
        self._ready_event = asyncio.Event()
        self._connected = False
        self._last_error: str | None = None
        self._socket: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._runtime: Path | None = None
        self._secret = secrets.token_urlsafe(32)
        self._pending: dict[str, asyncio.Future] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._owners: OrderedDict[str, str] = OrderedDict()
        self._log_tasks: list[asyncio.Task] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def bot_user(self) -> str:
        return "Nova Rootapp"

    async def wait_for_ready(self, timeout=45) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout)
        except TimeoutError:
            return False
        return self.is_connected

    async def _status(self, text: str) -> None:
        if self._on_status:
            await self._on_status(text)

    async def _listen(self) -> str:
        app = web.Application(client_max_size=256_000)
        app.router.add_get("/rootapp", self._accept)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/rootapp"

    async def _accept(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=256_000)
        await socket.prepare(request)
        try:
            auth = await socket.receive_json(timeout=5)
            supplied = auth.get("secret", "") if isinstance(auth, dict) else ""
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self._secret):
                await socket.close(code=1008, message=b"Unauthorized")
                return socket
            if self._socket is not None:
                await socket.close(code=1008, message=b"Already connected")
                return socket
            self._socket = socket
            await socket.send_json({"type": "connected"})
            async for frame in socket:
                if frame.type != WSMsgType.TEXT:
                    continue
                try:
                    body = json.loads(frame.data)
                    if not isinstance(body, dict):
                        continue
                    if body.get("type") == "ready":
                        self._connected = True
                        self._last_error = None
                        self._ready_event.set()
                    elif body.get("type") == "result":
                        future = self._pending.get(str(body.get("id", "")))
                        if future is not None and not future.done():
                            if body.get("error"):
                                future.set_exception(
                                    RuntimeError("Root SDK request failed: " + str(body["error"]))
                                )
                            else:
                                future.set_result(str(body.get("message_id", "")))
                    elif body.get("type") == "message" and self.is_connected:
                        await self._incoming(body)
                except (ValueError, TypeError):
                    continue
        except (TimeoutError, ValueError, TypeError):
            await socket.close(code=1008)
        finally:
            if self._socket is socket:
                self._socket = None
                self._connected = False
                self._fail_pending()
        return socket

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Rootapp disconnected"))

    async def _drain_diagnostics(self, reader: asyncio.StreamReader) -> None:
        """Classify SDK output without displaying token-bearing SDK objects."""
        recent = ""
        failures = {
            "eaddrinuse": "Root DevHost could not bind its ports. Stop other DevHosts and retry.",
            "cannot find module": "Root DevHost could not load its SDK. Run /remote setup rootapp again.",
            "unauthenticated": "Root rejected the Dev Token. Generate a new token for this bot and community.",
            "unauthorized": "Root rejected the Dev Token or community access.",
            "rootapp lifecycle failed": "Rootapp lifecycle failed. Check Dev Token, Bot ID and community permissions.",
        }
        while chunk := await reader.read(4096):
            recent = (recent + chunk.decode(errors="replace").lower())[-8192:]
            for marker, explanation in failures.items():
                if marker in recent:
                    self._last_error = explanation
                    self._ready_event.set()
                    break

    async def _rpc(self, method: str, channel: str, **fields) -> str:
        if not self.is_connected or self._socket is None:
            raise RuntimeError("Rootapp is not connected")
        request_id = secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._socket.send_json(
                {
                    "type": "command",
                    "id": request_id,
                    "method": method,
                    "channel_id": channel,
                    **fields,
                }
            )
            return await asyncio.wait_for(future, 15)
        finally:
            self._pending.pop(request_id, None)

    async def _incoming(self, body: dict) -> None:
        channel, user, mid = (str(body.get(key, "")) for key in ("channel_id", "user_id", "id"))
        allowed = {str(self._config.chat_id), *map(str, self._config.allowed_ids)}
        if channel not in allowed:
            return
        if user not in {str(value) for value in self._config.allowed_user_ids}:
            # Local notice only: do not acknowledge unauthorized senders in Root.
            await self._status(
                f"Rootapp blocked sender {user}. In Nova: /remote allow-user rootapp {user}"
            )
            return
        text = body.get("text")
        if not mid or mid in self._seen or not isinstance(text, str) or not text.strip():
            return
        self._seen[mid] = None
        if len(self._seen) > 2000:
            self._seen.popitem(last=False)
        route: dict[str, str] = {}
        message = self._target(channel, route)
        message.user_name = user
        message.sender_id = user
        message.text = text.strip()
        parents = body.get("parent_ids", [])
        if isinstance(parents, list):
            message.reply_to_owner = next(
                (self._owners[p] for p in parents if isinstance(p, str) and p in self._owners), None
            )
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            await self._status("Rootapp message queue is full; incoming request was dropped.")

    def _target(self, channel: str, route: dict) -> RemoteMessage:
        async def send(content):
            mid = await self._rpc("create", channel, content=content)
            self._owners[mid] = str(route.get("sid", "root"))
            if len(self._owners) > 2000:
                self._owners.popitem(last=False)
            return mid

        async def edit(mid, content):
            await self._rpc("edit", channel, message_id=mid, content=content)

        async def delete(mid):
            await self._rpc("delete", channel, message_id=mid)

        def editor():
            return connected_editor(
                self,
                PagedEditor(
                    lambda text: chunk_message(text, RemotePlatform.ROOTAPP),
                    send,
                    edit,
                    delete,
                ),
            )

        async def reply(text):
            for page in chunk_message(text, RemotePlatform.ROOTAPP):
                await send(page)

        async def typing():
            await self._rpc("typing", channel)

        async def ask(text):
            ids = [await send(page) for page in chunk_message(text, RemotePlatform.ROOTAPP)]

            async def remove():
                for mid in ids:
                    await delete(mid)

            return remove

        return RemoteMessage(
            RemotePlatform.ROOTAPP,
            channel,
            "Nova",
            "",
            reply,
            typing_fn=typing,
            edit_fn=editor(),
            answer_edit_fn=editor(),
            ask_fn=ask,
            route=route,
        )

    async def stream_target(self, *, sid="root") -> RemoteMessage | None:
        return self._target(str(self._config.chat_id), {"sid": sid}) if self.is_connected else None

    def _prepare_runtime(self, url: str) -> Path:
        from novacode_cli.security.secret_files import write_secret_json

        # A unique directory lets multiple Nova sessions share the installed SDK.
        runtime = Path(tempfile.mkdtemp(prefix="session-", dir=sdk_home()))
        self._runtime = runtime
        for name in ("bot.cjs", "transport.cjs"):
            shutil.copyfile(ASSETS / name, runtime / name)
        write_secret_json(runtime / "connection.json", {"url": url, "secret": self._secret})
        (runtime / "root-manifest.json").write_text(
            json.dumps(
                {
                    "id": self._config.bot_id,
                    "version": "1.0.0",
                    "package": {
                        "server": {
                            "launch": "bot.cjs",
                            "deploy": ["*.cjs"],
                            "nodeModules": ["node_modules"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return runtime

    @staticmethod
    def _host_ports() -> dict[str, str]:
        """DevHost's default fixed ports collide across Nova sessions."""
        names = (
            "ROOT_DEV_GRPC_PORT",
            "ROOT_DEV_HTTP_PORT",
            "ROOT_DEV_WS_PORT",
            "ROOT_DEV_WS_UPDATE_PORT",
        )
        with contextlib.ExitStack() as stack:
            ports = {}
            for name in names:
                reservation = stack.enter_context(socket.socket())
                reservation.bind(("127.0.0.1", 0))
                ports[name] = str(reservation.getsockname()[1])
            return ports

    async def run(self) -> None:
        try:
            node = node_executable()
            if not devhost_entry().is_file():
                raise RuntimeError("Install Root's SDK first: /remote setup rootapp")
            url = await self._listen()
            runtime = await asyncio.to_thread(self._prepare_runtime, url)
            self._process = await asyncio.create_subprocess_exec(
                node,
                str(devhost_entry()),
                cwd=runtime,
                env={
                    **os.environ,
                    **self._host_ports(),
                    "DEV_GRPC_IP": "127.0.0.1",
                    "DEV_TOKEN": self._config.token,
                    # DevHost enables the child inspector; keep its port unique.
                    "NODE_OPTIONS": (
                        os.environ.get("NODE_OPTIONS", "") + " --inspect-port=0"
                    ).strip(),
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_options(),
            )
            self._log_tasks = [
                asyncio.create_task(self._drain_diagnostics(reader))
                for reader in (self._process.stdout, self._process.stderr)
                if reader is not None
            ]
            code = await self._process.wait()
            self._last_error = self._last_error or (
                f"Root DevHost exited ({code}). Check Dev Token, Bot ID and community permissions."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = (
                str(exc).replace(self._config.token, "[redacted]")
                if self._config.token
                else str(exc)
            )
        finally:
            self._ready_event.set()
            await self.stop()

    async def stop(self) -> None:
        self._connected = False
        if self._socket is not None:
            await self._socket.close()
        self._fail_pending()
        if self._process is not None and self._process.returncode is None:
            await stop_process(self._process)
        for task in self._log_tasks:
            task.cancel()
        await asyncio.gather(*self._log_tasks, return_exceptions=True)
        self._log_tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._runtime is not None:
            # Only remove the two ephemeral credential/manifest files; do not
            # recursively delete a computed directory or touch installed SDKs.
            for name in ("connection.json", "root-manifest.json", "bot.cjs", "transport.cjs"):
                await asyncio.to_thread((self._runtime / name).unlink, missing_ok=True)
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._runtime.rmdir)
            self._runtime = None
