"""Temporary public links for Nova's local web interfaces, via ngrok."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from novacode_cli.config.config import console

_URL = re.compile(r"https://[a-zA-Z0-9-]+\.ngrok(?:-[a-zA-Z0-9-]+)?\.(?:app|io|dev)")
_READY_TIMEOUT = 30.0


@dataclass
class _Tunnel:
    service: str
    port: int
    process: subprocess.Popen[str]
    public_origin: str | None = None
    log_tail: list[str] | None = None
    server: Any = None
    public_url: str | None = None


class ShareManager:
    """Own short-lived ngrok processes and their exact local-origin grants."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tunnels: dict[str, _Tunnel] = {}

    def start(self, service: str, port: int, *, server: Any = None) -> str:
        with self._lock:
            current = self._tunnels.get(service)
            if (
                current
                and current.port == port
                and current.process.poll() is None
                and current.public_origin
            ):
                self._allow_origin(server, current.public_origin, True)
                return current.public_origin
            if current:
                self._stop_locked(service)

            executable = shutil.which("ngrok")
            if not executable:
                raise RuntimeError(
                    "ngrok is required for temporary sharing. Install it from "
                    "https://ngrok.com/download, add your account token with "
                    "`ngrok config add-authtoken <token>`, then run /share again."
                )

            args = [
                executable,
                "http",
                f"http://127.0.0.1:{port}",
                f"--host-header=127.0.0.1:{port}",
                f"--web-addr=127.0.0.1:{self._free_port()}",
                "--log=stdout",
                "--log-format=json",
            ]
            options: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(args, **options)
            tunnel = _Tunnel(service, port, process, log_tail=[], server=server)
            self._tunnels[service] = tunnel
            ready = threading.Event()
            reader = threading.Thread(
                target=self._read_output,
                args=(tunnel, ready),
                daemon=True,
                name=f"nova-share-{service}",
            )
            reader.start()

        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if tunnel.public_origin:
                self._allow_origin(server, tunnel.public_origin, True)
                return tunnel.public_origin
            if process.poll() is not None:
                break
            ready.wait(0.1)

        with self._lock:
            self._stop_locked(service)
        details = "\n".join(tunnel.log_tail or [])[-1200:]
        suffix = f" ngrok output: {details}" if details else ""
        raise RuntimeError(f"ngrok did not create a public URL.{suffix}")

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _read_output(tunnel: _Tunnel, ready: threading.Event) -> None:
        stream = tunnel.process.stdout
        if stream is None:
            return
        for line in stream:
            if tunnel.log_tail is not None:
                tunnel.log_tail.append(line.rstrip())
                del tunnel.log_tail[:-20]
            values: list[str] = []
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    values.extend(
                        str(record.get(key, ""))
                        for key in ("url", "public_url", "msg")
                    )
            except json.JSONDecodeError:
                values.append(line)
            for value in values:
                match = _URL.search(value)
                if match:
                    tunnel.public_origin = match.group(0).rstrip("/")
                    ready.set()

    @staticmethod
    def _allow_origin(server: Any, origin: str, allow: bool) -> None:
        httpd = getattr(server, "_server", None)
        if httpd is None:
            return
        origins = set(getattr(httpd, "nova_share_origins", set()))
        if allow:
            origins.add(origin)
        else:
            origins.discard(origin)
        httpd.nova_share_origins = origins

    def stop(self, service: str) -> bool:
        with self._lock:
            return self._stop_locked(service)

    def _stop_locked(self, service: str) -> bool:
        tunnel = self._tunnels.pop(service, None)
        if tunnel is None:
            return False
        self._allow_origin_for_tunnel(tunnel, False)
        if tunnel.process.poll() is None:
            tunnel.process.terminate()
            try:
                tunnel.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel.process.kill()
                tunnel.process.wait(timeout=3)
        return True

    def _allow_origin_for_tunnel(self, tunnel: _Tunnel, allow: bool) -> None:
        # The server object is attached by the command after startup, because
        # only the Create UI uses LocalRequestHandler's origin allowlist.
        server = getattr(tunnel, "server", None)
        if server is not None and tunnel.public_origin:
            self._allow_origin(server, tunnel.public_origin, allow)

    def stop_all(self) -> None:
        with self._lock:
            for service in tuple(self._tunnels):
                self._stop_locked(service)

    def list(self) -> dict[str, str]:
        with self._lock:
            return {
                name: tunnel.public_url or tunnel.public_origin
                for name, tunnel in self._tunnels.items()
                if tunnel.public_origin and tunnel.process.poll() is None
            }

    def set_public_url(self, service: str, url: str) -> None:
        with self._lock:
            tunnel = self._tunnels.get(service)
            if tunnel is not None:
                tunnel.public_url = url


_manager = ShareManager()
atexit.register(_manager.stop_all)


def stop_share(service: str) -> bool:
    """Stop one named share and revoke its public URL."""
    return _manager.stop(service)


def _local_origin(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.port is None:
        raise RuntimeError("The local web server did not provide a port.")
    return f"http://127.0.0.1:{parsed.port}", parsed.port


async def handle_share_command(ctx) -> bool:
    """Start/stop temporary public links for an existing Nova web interface."""
    args = (ctx.cmd_args or "").split()
    if not args or args[0].lower() in {"list", "status"}:
        active = _manager.list()
        if active:
            console.print("\n[bold]Temporary Nova shares[/bold]")
            for name, url in active.items():
                console.print(f"  [cyan]{name}[/cyan]  {url}")
            console.print(
                "[dim]Anyone with these links can open them; use /share stop "
                "to revoke access.[/dim]\n"
            )
        else:
            console.print(
                "[dim]No active shares. Use /share artifacts|council|cowork|create.[/dim]"
            )
        return True

    service = args[0].lower()
    if service in {"stop", "revoke"}:
        if len(args) > 1 and args[1].lower() != "all":
            stopped = _manager.stop(args[1].lower())
            message = (
                "[green]Temporary share stopped.[/green]"
                if stopped
                else "[dim]No share was active for that page.[/dim]"
            )
            console.print(message)
        else:
            _manager.stop_all()
            console.print("[green]All temporary shares stopped.[/green]")
        return True

    if service not in {"artifacts", "council", "cowork", "create"}:
        console.print(
            "[yellow]Usage: /share [artifacts|council|cowork|create|list|stop [name|all]][/yellow]"
        )
        return True

    try:
        local_url, server = await _get_local_page(service, ctx)
        _, port = _local_origin(local_url)
        public_origin = await asyncio.to_thread(
            _manager.start, service, port, server=server
        )
        if service == "create":
            token = server._server.auth_token
            public_url = f"{public_origin}/?token={token}"
        elif service == "cowork":
            from urllib.parse import parse_qsl, urlencode, urlsplit

            parsed = urlsplit(local_url)
            public_url = f"{public_origin}{parsed.path}?{urlencode(dict(parse_qsl(parsed.query)))}"
        else:
            path = "/" if service in {"artifacts", "council"} else ""
            public_url = public_origin + path
        _manager.set_public_url(service, public_url)
        console.print(
            f"\n[green]Temporary {service} share is live:[/green]\n"
            f"[bold cyan]{public_url}[/bold cyan]"
        )
        console.print(
            "[dim]Anyone with this link can access it. Cowork can operate on your "
            "workspace; Council can run model calls. Use /share stop to revoke it; "
            "it also ends when Nova exits.[/dim]\n"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not share {service}: {exc}[/red]")
    return True


async def _get_local_page(service: str, ctx) -> tuple[str, Any]:
    if service == "artifacts":
        from novacode_cli.artifacts.server import ensure_server

        return ensure_server(), None
    if service == "council":
        from novacode_cli.commands.chat_handler import set_agent_refs, start_chat_server

        set_agent_refs(ctx.agent, ctx.assistant_id, ctx.session_state, asyncio.get_running_loop())
        return start_chat_server(), None
    if service == "cowork":
        from novacode_cli.cowork.launcher import cowork_url

        return cowork_url(), None

    from novacode_cli.commands.create_server import CreateServer

    server = getattr(ctx.session_state, "create_server", None)
    if not server or not server.is_running:
        server = CreateServer()
        await server.start()
        ctx.session_state.create_server = server
    return server.url, server


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_share_command(ctx)

    registry.register("share", _handle)
