"""Temporary public links for Nova's local web interfaces, via ngrok."""

from __future__ import annotations

import asyncio
import atexit
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from novacode_cli.config.config import console

_URL = re.compile(r"https://[a-zA-Z0-9-]+\.ngrok(?:-[a-zA-Z0-9-]+)?\.(?:app|io|dev)")
_READY_TIMEOUT = 30.0
_SHARE_SERVICES = ("artifacts", "council", "cowork", "create")
_DASHBOARD_TITLE = "Nova Share Dashboard"


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

    def origin(self, service: str) -> str | None:
        """Return the endpoint origin even when its advertised URL is a page path."""
        with self._lock:
            tunnel = self._tunnels.get(service)
            if tunnel is None or tunnel.process.poll() is not None:
                return None
            return tunnel.public_origin


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


def _dashboard_html(links: dict[str, str | None]) -> str:
    """Build the compact link hub shown inside the artifact viewer sandbox."""
    descriptions = {
        "artifacts": ("◆", "Artifacts", "Browse interactive previews and project notes."),
        "council": ("◉", "Council", "Compare perspectives and follow live deliberation."),
        "cowork": ("⌘", "Cowork", "Work with an agent in the current workspace."),
        "create": ("✳", "Create", "Edit and manage your skills and agents."),
    }
    cards = []
    for name in _SHARE_SERVICES:
        glyph, title, description = descriptions[name]
        url = links.get(name)
        action = (
            f'<a class="open" href="{html.escape(url, quote=True)}" '
            'target="_blank" rel="noopener">Open page <span aria-hidden="true">↗</span></a>'
            if url
            else '<span class="offline">Not shared yet</span>'
        )
        cards.append(
            f'<article class="card {"available" if url else "offline-card"}">'
            f'<div class="card-top"><span class="glyph">{glyph}</span>'
            f'<span class="state"><i></i>{"Live" if url else "Offline"}</span></div>'
            f'<h2>{title}</h2><p>{description}</p>{action}</article>'
        )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nova · Shared workspace</title>
<style>
:root{color-scheme:dark;--bg:#0c1014;--panel:#111820;--line:#26313b;--text:#eef2ed;
--muted:#98a6a2;--mint:#9ee6c0;--amber:#e8bd83}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:min(980px,100% - 48px);margin:0 auto;padding:clamp(30px,8vw,86px) 0 48px}
.topline{display:flex;align-items:center;gap:9px;color:var(--mint);font:600 11px/1.2 ui-monospace,monospace;
letter-spacing:.15em;text-transform:uppercase}.mark{display:grid;place-items:center;width:27px;height:27px;
border:1px solid #44665a;border-radius:8px;font-size:15px}
h1{max-width:620px;margin:31px 0 12px;font-size:clamp(38px,8vw,68px);font-weight:540;
letter-spacing:-.065em;line-height:.99} .intro{max-width:540px;margin:0;color:var(--muted);font-size:15px}
.rule{height:1px;margin:34px 0 18px;background:var(--line)}
.section-label{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;
color:var(--muted);font:11px ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.card{min-height:205px;display:flex;flex-direction:column;padding:20px 21px 18px;
border:1px solid var(--line);border-radius:13px;background:var(--panel)}
.card-top{display:flex;align-items:center;justify-content:space-between}
.glyph{display:grid;place-items:center;width:35px;height:35px;border-radius:10px;
background:#1b2a27;color:var(--mint);font-size:17px}
.state{display:flex;align-items:center;gap:7px;color:var(--mint);font:11px ui-monospace,monospace}
.state i{width:6px;height:6px;border-radius:50%;background:var(--mint);box-shadow:0 0 10px #9ee6c077}
.offline-card .glyph{background:#202328;color:#87908e}.offline-card .state{color:#87908e}
.offline-card .state i{background:#68716f;box-shadow:none}
h2{margin:20px 0 4px;font-size:18px;font-weight:570;letter-spacing:-.025em}
.card p{margin:0;color:var(--muted);font-size:13px}.open,.offline{margin-top:auto;padding-top:19px;
font-size:12px;font-weight:600}.open{color:var(--mint);text-decoration:none;width:max-content}
.open:hover{text-decoration:underline;text-underline-offset:4px}.open:focus-visible{outline:2px solid var(--mint);
outline-offset:4px;border-radius:2px}.offline{color:#78827f}
footer{display:flex;justify-content:space-between;gap:12px;margin-top:22px;color:#76817d;
font:10px ui-monospace,monospace;letter-spacing:.04em}
@media(max-width:620px){main{width:calc(100% - 32px);padding-top:36px}h1{margin-top:25px}
.grid{grid-template-columns:1fr;gap:9px}.card{min-height:178px;padding:17px}.card h2{margin-top:15px}
footer{flex-direction:column;gap:5px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
</style></head><body><main>
<div class="topline"><span class="mark">N</span> NovaCode <span>／</span> temporary links</div>
<h1>Your Nova<br>workspace.</h1>
<p class="intro">One place to open the pages shared from this session. Links stay available while Nova is running and their shares remain active.</p>
<div class="rule"></div><div class="section-label"><span>Shared pages</span><span>Session links</span></div>
<section class="grid" aria-label="Shared Nova pages">""" + "".join(cards) + """</section>
<footer><span>NOVACODE · PRIVATE SESSION LINKS</span><span>Stop access any time with /share stop</span></footer>
</main></body></html>"""


def _publish_dashboard() -> str | None:
    """Create or refresh the share dashboard whenever the artifact tunnel is live."""
    artifact_origin = _manager.origin("artifacts")
    if not artifact_origin:
        return None

    links: dict[str, str | None] = {name: None for name in _SHARE_SERVICES}
    links["artifacts"] = f"{artifact_origin}/"
    for name in _SHARE_SERVICES[1:]:
        links[name] = _manager.list().get(name)

    from novacode_cli.artifacts.registry import get_registry
    from novacode_cli.artifacts.server import artifact_url

    registry = get_registry()
    existing = next(
        (artifact for artifact in registry.list() if artifact.title == _DASHBOARD_TITLE),
        None,
    )
    content = _dashboard_html(links)
    artifact = (
        registry.update(existing.id, type="html", content=content)
        if existing
        else registry.create(_DASHBOARD_TITLE, "html", content)
    )
    if artifact is None:
        return None
    url = f"{artifact_origin}/artifacts/{artifact.id}"
    _manager.set_public_url("artifacts", url)
    # Ensure the local viewer exists before publishing its deep link.
    artifact_url(artifact.id)
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
                "[dim]No active shares. Use /share artifacts|council|cowork|create.[/dim]"
            )
        return True

    service = args[0].lower()
    if service in {"stop", "revoke"}:
        if len(args) > 1 and args[1].lower() != "all":
            target = args[1].lower()
            stopped = _manager.stop(target)
            if stopped and target != "artifacts":
                _publish_dashboard()
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
            "[yellow]Usage: /share [all|artifacts|council|cowork|create|list|stop [name|all]][/yellow]"
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

    dashboard_url = _publish_dashboard()
    if service == "all":
        if shared:
            console.print("\n[bold]Nova workspace shares[/bold]")
            for name, url in shared.items():
                console.print(f"  [cyan]{name}[/cyan]  {url}")
        if failures:
            console.print("\n[yellow]Some pages could not be shared:[/yellow]")
            for name, error in failures.items():
                console.print(f"  [red]{name}:[/red] {error}")
        if dashboard_url:
            console.print(
                f"\n[green]Workspace dashboard:[/green]\n[bold cyan]{dashboard_url}[/bold cyan]"
            )
        console.print(
            "\n[dim]These links work while Nova is running. Anyone with a link can "
            "open that page; Cowork can operate on your workspace and Council can "
            "run model calls. Use /share stop to revoke all links.[/dim]\n"
        )
    else:
        if service in shared:
            console.print(
                f"\n[green]Temporary {service} share is live:[/green]\n"
                f"[bold cyan]{shared[service]}[/bold cyan]"
            )
        else:
            console.print(f"[red]Could not share {service}: {failures[service]}[/red]")
        if dashboard_url:
            console.print(
                f"[green]Workspace dashboard:[/green] [bold cyan]{dashboard_url}[/bold cyan]"
            )
        console.print(
            "[dim]Anyone with a share link can access that page. Use /share stop "
            "to revoke links; they also end when Nova exits.[/dim]\n"
        )
    return True


async def _start_share(service: str, ctx) -> str:
    """Start one page's tunnel and return its visitor URL."""
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
        public_url = public_origin + "/"
    _manager.set_public_url(service, public_url)
    return public_url


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
