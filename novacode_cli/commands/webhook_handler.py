"""``/webhook`` command — manage the webhook ingress server (Enhancement 5).

Subcommands::

    /webhook start [--port 9876]
    /webhook stop
    /webhook register github --secret <secret> --events push,pull_request
    /webhook status

The server (:class:`~novacode_cli.remote.webhook_server.WebhookServer`) accepts
``POST /webhook/{source}`` and, on a verified signature, enqueues a run. Sources:
``github``, ``linear``, ``generic``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from novacode_cli.remote.webhook_adapters import ADAPTERS

if TYPE_CHECKING:
    from rich.console import Console

    from novacode_cli.remote.webhook_server import WebhookServer
    from novacode_cli.states.Session import SessionState

_DEFAULT_PORT = 9876


def _parse_flags(tokens: list[str]) -> dict[str, str]:
    """Parse ``--key value`` flag pairs from a token list."""
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and i + 1 < len(tokens):
            flags[tok[2:]] = tokens[i + 1]
            i += 2
        else:
            i += 1
    return flags


def _get_server(session_state: SessionState, *, port: int = _DEFAULT_PORT) -> WebhookServer:
    """Return the session's webhook server, creating it on first use."""
    server = getattr(session_state, "_webhook_server", None)
    if server is None:
        import asyncio

        from novacode_cli.memory.store import get_durable_store
        from novacode_cli.remote.webhook_server import WebhookServer

        queue = getattr(session_state, "_remote_message_queue", None)
        if queue is None:
            queue = asyncio.Queue(maxsize=100)
            session_state._remote_message_queue = queue
        server = WebhookServer(queue, store=get_durable_store(), port=port)
        session_state._webhook_server = server
    return server


async def handle_webhook_command(  # noqa: PLR0911 — command dispatcher, one return per subcommand
    cmd_args: str | None, session_state: SessionState, console: Console
) -> bool:
    """Dispatch a ``/webhook`` subcommand. Returns ``True`` (command handled)."""
    try:
        tokens = shlex.split((cmd_args or "").strip())
    except ValueError as exc:
        console.print(f"[red]Could not parse arguments: {exc}[/red]")
        return True

    action = tokens[0] if tokens else "status"
    rest = tokens[1:]

    if action == "start":
        flags = _parse_flags(rest)
        port = int(flags.get("port", _DEFAULT_PORT))
        server = _get_server(session_state, port=port)
        if server.running:
            console.print("  [dim]Webhook server already running.[/dim]")
            return True
        try:
            await server.start()
        except OSError as exc:
            console.print(f"[red]Could not start webhook server:[/red] {exc}")
            return True
        console.print(f"  [green]✓[/green] Webhook server listening — {server.url}")
        console.print(f"  [dim]Registered sources: {', '.join(ADAPTERS)}[/dim]")
        return True

    if action == "stop":
        server = getattr(session_state, "_webhook_server", None)
        if server is None or not server.running:
            console.print("  [dim]Webhook server is not running.[/dim]")
            return True
        await server.stop()
        console.print("  [green]✓[/green] Webhook server stopped")
        return True

    if action == "register":
        if not rest:
            console.print(
                "[yellow]Usage:[/yellow] /webhook register <source> "
                "--secret <secret> [--events e1,e2]"
            )
            return True
        source = rest[0]
        if source not in ADAPTERS:
            console.print(
                f"[red]Unknown source[/red] '{source}'. Choose from: {', '.join(ADAPTERS)}"
            )
            return True
        flags = _parse_flags(rest[1:])
        secret = flags.get("secret", "")
        if not secret:
            console.print("[yellow]A --secret is required to register a source.[/yellow]")
            return True
        events = [e for e in flags.get("events", "").split(",") if e]
        server = _get_server(session_state)
        await server.register_source(source, secret, event_types=events)
        console.print(
            f"  [green]✓[/green] Registered [cyan]{source}[/cyan]"
            + (f" for events: {', '.join(events)}" if events else " (all events)")
        )
        return True

    # No subcommand: report current status.
    server = getattr(session_state, "_webhook_server", None)
    if server is not None and server.running:
        console.print(f"  [green]●[/green] Webhook server running — {server.url}")
    else:
        console.print("  [dim]Webhook server stopped. Start it with /webhook start.[/dim]")
    return True
