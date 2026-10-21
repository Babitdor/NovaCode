"""Temporary public hosting for Nova session artifacts."""

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

_URL = re.compile(
    r"https://(?:[a-zA-Z0-9-]+\.ngrok(?:-[a-zA-Z0-9-]+)?\.(?:app|io|dev)"
    r"|[a-zA-Z0-9-]+\.trycloudflare\.com)"
)
_READY_TIMEOUT = 30.0
_SHARE_SERVICES = ("artifacts",)


@dataclass
class _Tunnel:
    service: str
    port: int
    process: subprocess.Popen[str]
    public_origin: str | None = None
    log_tail: list[str] | None = None
    server: Any = None
    public_url: str | None = None
    session_id: str | None = None
    provider: str = "ngrok"


class ShareManager:
    """Own short-lived tunnel processes and their exact local-origin grants."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._start_locks: dict[str, threading.Lock] = {}
        self._tunnels: dict[str, _Tunnel] = {}

    def start(
        self,
        service: str,
        port: int,
        *,
        server: Any = None,
        session_id: str | None = None,
    ) -> str:
        with self._lock:
            start_lock = self._start_locks.setdefault(service, threading.Lock())
        with start_lock:
            return self._start_once(
                service, port, server=server, session_id=session_id
            )

    def _start_once(
        self,
        service: str,
        port: int,
        *,
        server: Any = None,
        session_id: str | None = None,
    ) -> str:
        with self._lock:
            current = self._tunnels.get(service)
            if (
                current
                and current.port == port
                and current.process.poll() is None
                and current.public_origin
            ):
                if session_id:
                    current.session_id = session_id
                self._allow_origin(server, current.public_origin, True)
                return current.public_origin
            if current:
                self._stop_locked(service)

            # Artifact hosting must use Cloudflare, never silently switch providers.
            providers = ("cloudflared",) if service == "artifacts" else ("ngrok",)
            provider_name = None
            executable = None
            for candidate in providers:
                executable = shutil.which(candidate)
                if executable:
                    provider_name = candidate
                    break
            if not executable:
                if service == "council":
                    raise RuntimeError(
                        "Council uses Server-Sent Events, which Cloudflare Quick Tunnels "
                        "do not support. Install ngrok and configure its auth token to "
                        "share Council."
                    )
                if service == "artifacts":
                    raise RuntimeError(
                        "Artifact sharing needs `cloudflared`. Install "
                        "cloudflared from https://developers.cloudflare.com/tunnel/downloads/ "
                        "to use the free Cloudflare Quick Tunnel."
                    )
                raise RuntimeError(
                    "ngrok is required for temporary sharing of this page. Install it "
                    "from https://ngrok.com/download and configure its auth token."
                )

            if provider_name == "cloudflared":
                args = [
                    executable,
                    "tunnel",
                    "--url",
                    f"http://127.0.0.1:{port}",
                ]
            else:
                args = [
                    executable,
                    "http",
                    f"http://127.0.0.1:{port}",
                    f"--host-header=127.0.0.1:{port}",
                    "--inspect=false",
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
            tunnel = _Tunnel(
                service,
                port,
                process,
                log_tail=[],
                server=server,
                session_id=session_id,
                provider=provider_name or "ngrok",
            )
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
        suffix = f" Tunnel output: {details}" if details else ""
        raise RuntimeError(f"Tunnel did not create a public URL.{suffix}")

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

    def stop_session(self, session_id: str, *, exclude: frozenset[str] = frozenset()) -> set[str]:
        """Stop tunnels owned by a session, optionally preserving selected services."""
        if not session_id:
            return set()
        with self._lock:
            stopped = {
                name
                for name, tunnel in self._tunnels.items()
                if tunnel.session_id == session_id and name not in exclude
            }
            for service in stopped:
                self._stop_locked(service)
            return stopped

    def adopt(self, service: str, session_id: str) -> bool:
        """Transfer a live tunnel to a resumed session."""
        with self._lock:
            tunnel = self._tunnels.get(service)
            if tunnel is None or tunnel.process.poll() is not None:
                return False
            tunnel.session_id = session_id
            return True

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


def stop_session_shares(
    session_id: str, *, exclude: tuple[str, ...] = ()
) -> set[str]:
    """Revoke every temporary endpoint owned by a session."""
    return _manager.stop_session(session_id, exclude=frozenset(exclude))


def _local_origin(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.port is None:
        raise RuntimeError("The local web server did not provide a port.")
    return f"http://127.0.0.1:{parsed.port}", parsed.port


def _wait_for_upstream(port: int, timeout: float = 4.0) -> None:
    """Verify a local page is accepting connections before publishing a tunnel."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(
        f"Nova's local page is not accepting connections at "
        f"http://127.0.0.1:{port}. Restart Nova and try /share again."
    ) from last_error


def ensure_share_for_url(
    service: str,
    local_url: str,
    *,
    session_id: str | None = None,
) -> str:
    """Expose an already running Nova page and return its visitor URL."""
    _, port = _local_origin(local_url)
    _wait_for_upstream(port)
    origin = _manager.start(
        service,
        port,
        session_id=session_id,
    )
    public_url = f"{origin}/"
    _manager.set_public_url(service, public_url)
    return public_url


def ensure_artifact_share(artifact_id: str | None = None, session_id: str | None = None) -> str:
    """Start/reuse the artifact tunnel and optionally return an artifact deep link."""
    from novacode_cli.artifacts.server import ensure_server

    origin = ensure_share_for_url(
        "artifacts",
        ensure_server(),
        session_id=session_id,
    ).rstrip("/")
    if artifact_id:
        url = f"{origin}/artifacts/{artifact_id}"
        _manager.set_public_url("artifacts", url)
    else:
        url = origin + "/"
    return url


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
                "[dim]No active shares. Use /share artifacts.[/dim]"
            )
        return True

    service = args[0].lower()
    if service in {"stop", "revoke"}:
        if len(args) > 1 and args[1].lower() != "all":
            target = args[1].lower()
            stopped = _manager.stop(target)
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

    if service != "all" and service not in _SHARE_SERVICES:
        console.print(
            "[yellow]Usage: /share all | artifacts | list | stop[/yellow]"
        )
        return True

    services = _SHARE_SERVICES if service == "all" else (service,)
    shared: dict[str, str] = {}
    failures: dict[str, str] = {}
    for target in services:
        try:
            shared[target] = await _start_share(target, ctx)
        except Exception as exc:  # noqa: BLE001 — keep starting the remaining pages
            failures[target] = str(exc)

    if service == "all":
        if shared:
            console.print("\n[bold]Nova workspace shares[/bold]")
            for name, url in shared.items():
                console.print(f"  [cyan]{name}[/cyan]  {url}")
        if failures:
            console.print("\n[yellow]Some pages could not be shared:[/yellow]")
            for name, error in failures.items():
                console.print(f"  [red]{name}:[/red] {error}")
        console.print(
            "\n[dim]These links work while Nova is running. Anyone with a link can "
            "open that page. Use /share stop to revoke all links.[/dim]\n"
        )
    else:
        if service in shared:
            console.print(
                f"\n[green]Temporary {service} share is live:[/green]\n"
                f"[bold cyan]{shared[service]}[/bold cyan]"
            )
        else:
            console.print(f"[red]Could not share {service}: {failures[service]}[/red]")
        console.print(
            "[dim]Anyone with a share link can access that page. Use /share stop "
            "to revoke links; they also end when Nova exits.[/dim]\n"
        )
    return True


async def _start_share(service: str, ctx) -> str:
    """Start one page's tunnel and return its visitor URL."""
    if service != "artifacts":
        raise ValueError(f"Sharing is not supported for {service!r}.")
    return await asyncio.to_thread(
        ensure_artifact_share,
        session_id=getattr(ctx.session_state, "session_id", None),
    )


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_share_command(ctx)

    registry.register("share", _handle)
