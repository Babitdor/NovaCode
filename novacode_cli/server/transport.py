"""Loopback TCP transport for the TUI server: one line-delimited JSON stream.

The server owns sessions; this module owns *reaching* it. One listener, one
`SessionHost` per connection, one writer task per connection so a slow client can
never block the agent turn that is producing frames.

Security posture, deliberately narrow:

* bind `127.0.0.1` only — never a public interface, never `0.0.0.0`;
* a token generated with :func:`secrets.token_hex` must match via
  :func:`secrets.compare_digest` before any session frame is served;
* nothing but `hello`/`auth` is accepted before the handshake completes, and a
  connection that has not finished it inside :data:`HANDSHAKE_TIMEOUT` is closed.

Framing and the frame catalogue live in `rust/PROTOCOL.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import TYPE_CHECKING, Any, Self

from novacode_cli.server import frames
from novacode_cli.server.children import ChildSessionManager
from novacode_cli.server.sessions import SessionCapacityError, SessionRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from novacode_cli.server.host import SessionHost

    # Where a session host writes its outgoing frames.
    FrameSink = Callable[[dict[str, Any]], None]

logger = logging.getLogger(__name__)

# A client that connects and then says nothing must not hold a connection (or a
# session) open: the socket is unauthenticated until `auth` arrives.
HANDSHAKE_TIMEOUT = 30.0

# Frames waiting for a slow client. Dropping here is better than unbounded
# growth: the transcript is re-derivable, a wedged process is not.
OUTBOX_SIZE = 512


class FrameSinkError(RuntimeError):
    """The connection is gone; nothing more can be written to it."""


class Connection:
    """One authenticated client: reader loop, outbox and the session it drives."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        token: str,
        host_factory: Callable[[FrameSink], Awaitable[SessionHost]],
        registry: SessionRegistry | None = None,
    ) -> None:
        """Wire one accepted socket to a lazily created session host.

        Args:
            reader: Socket reader, already limited to `frames.MAX_LINE`.
            writer: Socket writer.
            token: The token this connection must present.
            host_factory: Builds the session host for this connection. Called
                once, after authentication, with the connection's sink so the
                host can emit without knowing about sockets.
            registry: The server's session registry. With it, `sessions` lists
                every session the server holds and `attach` can switch this
                connection to another one; without it the connection serves the
                single session its factory built.
        """
        self._reader = reader
        self._writer = writer
        self._token = token
        self._host_factory = host_factory
        self._registry = registry
        self._outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(OUTBOX_SIZE)
        self._writer_task: asyncio.Task[None] | None = None
        self._closed = False
        # The host this connection may close. `host` can move to another session
        # via `attach`; closing someone else's session on disconnect would be a
        # bug, so ownership and attachment are tracked separately.
        self._owned: SessionHost | None = None
        self.host: SessionHost | None = None
        # Child sessions this connection spawned. Closing someone else's child is
        # refused: ownership is what makes `close_session` safe.
        self._children: ChildSessionManager | None = None
        self._owned_children: set[str] = set()

    # ── outbound ─────────────────────────────────────────────────────────
    def send(self, msg: dict[str, Any]) -> None:
        """Queue one frame for this client without blocking the caller.

        Args:
            msg: Envelope to write. A full outbox drops the frame and logs; a
                closed connection ignores it.
        """
        if self._closed:
            return
        try:
            self._outbox.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("dropped %s frame: outbox full", msg.get("t"))

    async def _drain(self) -> None:
        """Write queued frames in order until the outbox is closed."""
        while True:
            msg = await self._outbox.get()
            if msg is None:
                return
            try:
                self._writer.write(frames.encode_line(msg))
                await self._writer.drain()
            except (ConnectionError, OSError, frames.FrameError):
                logger.debug("client write failed", exc_info=True)
                self._closed = True
                return

    async def _flush(self) -> None:
        """Let queued frames reach the socket before the connection closes."""
        self._outbox.put_nowait(None)
        if self._writer_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._writer_task, timeout=2)

    # ── lifecycle ────────────────────────────────────────────────────────
    async def serve(self) -> None:
        """Run the handshake, then serve frames until the client goes away."""
        self._writer_task = asyncio.create_task(self._drain())
        try:
            if not await self._handshake():
                return
            await self._run()
        finally:
            attached = self.host
            owned = self._owned
            if attached is not None and attached is not owned:
                # Leaving a session we merely watched must not close it.
                attached.remove_sink(self.send)
            if self._children is not None:
                with contextlib.suppress(Exception):
                    await self._children.close_all()
            if owned is not None:
                with contextlib.suppress(Exception):
                    await owned.close()
            await self._flush()
            self._closed = True
            with contextlib.suppress(ConnectionError, OSError, Exception):
                self._writer.close()
                await self._writer.wait_closed()

    async def _read(self) -> dict[str, Any] | None:
        """Read one envelope; `None` on EOF, noise or an oversized line."""
        while True:
            try:
                raw = await self._reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                self.send(frames.error("frame exceeds the line limit", code="framing"))
                return None
            except (ConnectionError, OSError):
                # Reset by the peer is EOF: there is nothing left to read and
                # nothing to report. Logging it as an error would turn a normal
                # Ctrl-C into noise in the session's log.
                return None
            if not raw:
                return None
            msg = frames.decode_line(raw)
            if msg is not None:
                return msg
            # Noise is skipped, not fatal: a stray log line must not end the UI.
            # A loop, not recursion, so a peer sending garbage cannot exhaust the
            # stack before the timeout or the EOF does.
            logger.debug("skipping unparseable line from client")

    async def _handshake(self) -> bool:
        """Perform `hello` → `auth` → `ready`.

        Returns:
            `True` when the connection is authenticated and a host is attached.
        """
        try:
            return await asyncio.wait_for(self._handshake_steps(), timeout=HANDSHAKE_TIMEOUT)
        except TimeoutError:
            self.send(frames.error("handshake timed out", code="handshake"))
            return False

    async def _handshake_steps(self) -> bool:
        """The handshake body, run under a timeout by :meth:`_handshake`."""
        hello = await self._read()
        if hello is None:
            return False
        problem = frames.validate_client_frame(hello, authed=False)
        if problem or hello.get("t") != "hello":
            self.send(frames.error(problem or "expected hello", code="handshake"))
            return False
        self.send(frames.hello(role="server", name="novacode-cli"))

        auth = await self._read()
        if auth is None:
            return False
        if auth.get("t") != "auth":
            self.send(frames.error("expected auth", code="handshake"))
            return False
        token = auth.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(token, self._token):
            # Do not echo the token, and do not say whether it was shape or value.
            logger.warning("rejected client: bad token")
            self.send(frames.error("authentication failed", code="auth"))
            return False

        return await self._start_session()

    async def _start_session(self) -> bool:
        """Build and start this connection's host.

        Returns:
            `True` once the session is live; `False` when the server is full, in
            which case `start` has already told the client why.
        """
        self.host = await self._host_factory(self.send)
        self._owned = self.host
        self.host.use_registry(self._registry)
        try:
            await self.host.start()
        except SessionCapacityError:
            return False
        return True

    async def _run(self) -> None:
        """Dispatch client frames to the session host until EOF or `shutdown`."""
        assert self.host is not None  # noqa: S101 — set by _handshake_steps
        while True:
            msg = await self._read()
            if msg is None:
                return
            kind = msg.get("t")
            if kind == "shutdown":
                return
            try:
                await self._dispatch(msg)
            except Exception:  # noqa: BLE001 — one bad frame must not kill the session
                logger.debug("frame %s failed", kind, exc_info=True)
                self.send(frames.error(f"could not handle {kind} frame", code="frame"))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route one frame, keeping session-scoped frames at connection level.

        Args:
            msg: Parsed client envelope.
        """
        kind = msg.get("t")
        assert self.host is not None  # noqa: S101 — set by _handshake_steps
        if kind == "create_session":
            await self._create_child(
                str(msg.get("name") or "session"),
                msg.get("cwd") or None,
                msg.get("resume") or None,
            )
            return
        if kind == "close_session":
            await self._close_child(str(msg.get("session_id") or ""))
            return
        if kind == "attach":
            await self._attach(str(msg.get("session_id") or ""))
            return
        await _host_dispatch(self.host, msg)

    def _child_manager(self) -> ChildSessionManager | None:
        """Build this connection's child manager on first use."""
        if self._registry is None:
            return None
        if self._children is None:
            base = None
            spec = getattr(self._owned, "spec", None)
            if spec is not None:
                base = getattr(spec, "cwd", None)
            self._children = ChildSessionManager(self._registry, base)
        return self._children

    async def _create_child(
        self,
        name: str,
        cwd: Any,  # noqa: ANN401 — wire value
        resume: Any = None,  # noqa: ANN401 — wire value
    ) -> None:
        """Spawn a child session, register it, then attach this connection.

        Args:
            name: Display name for the tab.
            cwd: Optional workspace path from the client.
            resume: Optional saved session id the child should continue.
        """
        manager = self._child_manager()
        if manager is None:
            self.send(frames.error("Session spawning is unavailable.", code="sessions"))
            return
        try:
            host = await manager.create(
                name,
                cwd if isinstance(cwd, str) else None,
                resume_id=resume if isinstance(resume, str) else None,
            )
        except (RuntimeError, OSError, ValueError) as error:
            self.send(frames.error(f"Could not start a session: {error}", code="sessions"))
            return
        self._owned_children.add(host.session_id)
        await self._attach(host.session_id)

    async def _close_child(self, session_id: str) -> None:
        """Stop one child this connection spawned; never another client's.

        Args:
            session_id: Child requested by the client.
        """
        assert self.host is not None  # noqa: S101 — set by _handshake_steps
        if self._children is None or session_id not in self._owned_children:
            self.send(
                frames.notice(
                    "Only sessions this connection started can be closed.",
                    level="warning",
                )
            )
            return
        target = self.host
        was_active = target.session_id == session_id
        await self._children.close(session_id)
        self._owned_children.discard(session_id)
        if was_active and self._owned is not None:
            target.remove_sink(self.send)
            self.host = self._owned
            self._owned.add_sink(self.send)
            self.send(frames.notice("Closed the session tab."))
            await self._owned.list_sessions()
        else:
            self.send(frames.notice("Closed the session tab."))
            await self.host.list_sessions()

    async def _attach(self, session_id: str) -> None:
        """Point this connection at another session the server holds.

        Args:
            session_id: Session id to attach to.
        """
        registry = self._registry
        target = registry.get(session_id) if registry is not None and session_id else None
        if target is None:
            # Unknown id, or no registry: the host explains, and keeps the
            # session it has rather than leaving the client with none.
            assert self.host is not None  # noqa: S101
            await self.host.attach(session_id)
            return
        assert self.host is not None  # noqa: S101
        if target is self.host:
            self.send(frames.notice("Already attached to that session."))
            return
        # Move this client's sink: from now on the target's turn frames reach it,
        # and its own (now detached) session stops painting here.
        assert self.host is not None  # noqa: S101
        self.host.remove_sink(self.send)
        target.add_sink(self.send)
        self.host = target
        # Name and id both: two sessions may share a display name, and the id is
        # what the client keys its tab state by.
        name = str(target.row().get("name") or session_id)
        label = name if name == session_id else f"{name} ({session_id})"
        self.send(frames.notice(f"Attached to {label}."))
        # Re-announce the list so the client repaints its tab bar with the new
        # active session in the same turn as the notice.
        await target.list_sessions()


async def _host_dispatch(host: SessionHost, msg: dict[str, Any]) -> None:  # noqa: PLR0912
    """Route one authenticated client frame to the matching host method.

    Args:
        host: The connection's session host.
        msg: Parsed client envelope.
    """
    kind = msg.get("t")
    if kind == "prompt":
        await host.prompt(
            str(msg.get("id") or "p"),
            str(msg.get("text") or ""),
            images=msg.get("images") or [],
            auto_approve=msg.get("auto_approve"),
            steer=msg.get("steer") is True,
        )
    elif kind == "cancel":
        await host.cancel()
    elif kind == "interrupt_reply":
        await host.interrupt_reply(str(msg.get("id") or ""), msg.get("result"))
    elif kind == "screen_result":
        answer = frames.screen_answer(msg)
        if answer is not None:
            await host.resolve_screen(*answer)
    elif kind == "catalogue":
        await host.send_catalogue()
    elif kind == "command":
        await host.command(str(msg.get("id") or "c"), str(msg.get("name") or ""), msg.get("args"))
    elif kind == "sessions":
        await host.list_sessions()
    elif kind == "attach":
        await host.attach(str(msg.get("session_id") or ""))
    elif kind == "approval_mode":
        await host.set_approval_mode(msg.get("enabled"))
    elif kind == "pty_input":
        host.terminal_keys(str(msg.get("id") or ""), str(msg.get("text") or ""))
    elif kind == "pty_close":
        await host.terminal_close(str(msg.get("id") or "") or None)
    else:
        logger.debug("skipping unknown client frame %r", kind)


class TuiServer:
    """The loopback listener: binds before a client is spawned, closes on exit."""

    def __init__(
        self,
        host_factory: Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionHost]],
        *,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        registry: SessionRegistry | None = None,
    ) -> None:
        """Prepare a listener without binding it.

        Args:
            host_factory: Builds one session host per authenticated connection.
            token: Shared secret; generated when omitted.
            host: Interface to bind. Loopback by default and by policy.
            port: Port to bind, `0` for an ephemeral one.
            registry: The session registry shared by every connection this
                listener accepts, so clients can list and attach each other's
                sessions. A fresh one is created when omitted.
        """
        self._host_factory = host_factory
        # `or` would discard an empty registry that a caller configured with a
        # smaller capacity: an empty container is falsy.
        self.registry = registry if registry is not None else SessionRegistry()
        self.token = token or secrets.token_hex(16)
        self._host = host
        self._requested_port = port
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._port: int | None = None

    @property
    def port(self) -> int:
        """The bound port.

        Returns:
            The port the listener bound, after :meth:`start`.

        Raises:
            RuntimeError: When called before :meth:`start`.
        """
        if self._port is None:
            msg = "server has not been started"
            raise RuntimeError(msg)
        return self._port

    async def start(self) -> int:
        """Bind and begin accepting connections.

        Returns:
            The bound port (useful when constructed with `port=0`).
        """
        self._server = await asyncio.start_server(
            self._on_connect,
            self._host,
            self._requested_port,
            limit=frames.MAX_LINE,
        )
        socket = self._server.sockets[0] if self._server.sockets else None
        self._port = socket.getsockname()[1] if socket is not None else self._requested_port
        logger.info("tui server listening on %s:%s", self._host, self._port)
        return self._port

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Accept one client and hand it to a `Connection`.

        Args:
            reader: Accepted socket reader.
            writer: Accepted socket writer.
        """
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)  # type: ignore[arg-type]
        conn = Connection(
            reader,
            writer,
            token=self.token,
            host_factory=self._host_factory,
            registry=self.registry,
        )
        try:
            await conn.serve()
        except (ConnectionError, OSError):
            # A client that dies mid-frame is ordinary — the user hit Ctrl-C or
            # the terminal closed. Report nothing: a closed session is not a
            # server fault.
            logger.debug("client went away mid-connection")
        finally:
            if task is not None:
                self._connections.discard(task)  # type: ignore[arg-type]

    async def stop(self) -> None:
        """Stop accepting, then close every live connection."""
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
        for task in list(self._connections):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._connections.clear()
        logger.info("tui server stopped")

    async def __aenter__(self) -> Self:
        """Bind on entry so a client can be spawned against a live port.

        Returns:
            This server, already listening.
        """
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Close the listener and its connections on exit."""
        await self.stop()
