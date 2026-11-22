"""The socket half of the TUI server: handshake, auth, framing, teardown.

These tests use a real loopback socket (the same ``asyncio.start_server`` the
launcher uses) because the failure modes here are transport-level: a token that
must be compared in constant time, a frame that must be skipped rather than
fatal, a port that must be released when the client goes away.

No model, no API key, no subprocess: the session is a stand-in and the turn
stream is injected, exactly as ``tests/test_session_worker.py`` does.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.server import frames, transport
from novacode_cli.server.host import SessionHost, SessionSpec
from novacode_cli.server.transport import TuiServer

if TYPE_CHECKING:
    from collections.abc import Callable

# A fixture secret, not a credential.
TEST_SECRET = "test-token-0123456789"  # noqa: S105


class _SS:
    """Minimal session-state stand-in (mirrors tests/test_session_worker.py)."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos = None
        self.steering_instructions = []
        self.headless_out_fd = None


def _spec() -> SessionSpec:
    """Build a session spec that needs no agent."""
    return SessionSpec(
        agent=object(),
        session_state=_SS(),
        assistant_id="nova-agent",
        model_name="test-model",
        cwd="C:/work",
    )


async def _send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """Write one frame."""
    writer.write(frames.encode_line(msg))
    await writer.drain()


async def _recv(reader: asyncio.StreamReader, *, deadline: float = 5.0) -> dict[str, Any]:
    """Read the next frame, failing the test on EOF or garbage."""
    line = await asyncio.wait_for(reader.readline(), timeout=deadline)
    assert line, "server closed the connection"
    msg = frames.decode_line(line)
    assert msg is not None, f"unparseable frame: {line!r}"
    return msg


async def _recv_skipping(
    reader: asyncio.StreamReader, *skip: str, deadline: float = 5.0
) -> dict[str, Any]:
    """Read until a frame whose type is not in *skip* arrives."""
    while True:
        msg = await _recv(reader, deadline=deadline)
        if msg.get("t") not in skip:
            return msg


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict[str, Any]:
    """Complete hello/auth and return the ready frame."""
    await _send(writer, frames.hello(role="client", name="nova-tui/test"))
    hello = await _recv(reader)
    assert hello["t"] == "hello"
    await _send(writer, {"t": "auth", "token": TEST_SECRET})
    return await _recv_skipping(reader, "notice")


async def _serve(
    spec_factory: Callable[[], SessionSpec] | None = None,
) -> tuple[TuiServer, int]:
    """Start a server whose sessions are stand-ins."""

    async def host_factory(emit: Callable[[dict[str, Any]], None]) -> SessionHost:
        return SessionHost(emit, spec_factory or _spec)

    server = TuiServer(host_factory, token=TEST_SECRET)
    port = await server.start()
    return server, port


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a client connection to the test server."""
    return await asyncio.open_connection("127.0.0.1", port)


async def _close(writer: asyncio.StreamWriter, server: TuiServer) -> None:
    """Tear down a client and the server."""
    writer.close()
    await writer.wait_closed()
    await server.stop()


async def test_handshake_negotiates_and_reports_the_session() -> None:
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        await _send(writer, frames.hello(role="client", name="nova-tui/test"))
        hello = await _recv(reader)
        assert hello["t"] == "hello"
        assert hello["protocol"] == frames.PROTOCOL_VERSION
        await _send(writer, {"t": "auth", "token": TEST_SECRET})
        ready = await _recv(reader)
        assert ready["t"] == "ready"
        assert ready["ui"] == "ratatui"
        assert ready["session_id"] == "s1"
        assert ready["capacity"] >= 1
    finally:
        await _close(writer, server)


async def test_bad_token_gets_an_error_then_a_close() -> None:
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        await _send(writer, frames.hello(role="client", name="nova-tui/test"))
        assert (await _recv(reader))["t"] == "hello"
        await _send(writer, {"t": "auth", "token": "wrong"})
        error = await _recv(reader)
        assert error["t"] == "error"
        assert error["code"] == "auth"
        assert "wrong" not in error["message"]
        assert await asyncio.wait_for(reader.readline(), timeout=5) == b""
    finally:
        await _close(writer, server)


async def test_a_frame_before_auth_is_refused() -> None:
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        await _send(writer, {"t": "prompt", "id": "p1", "text": "hi"})
        error = await _recv(reader)
        assert error["t"] == "error"
        assert error["code"] == "handshake"
    finally:
        await _close(writer, server)


async def test_unknown_frame_is_skipped_not_fatal() -> None:
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        assert (await _handshake(reader, writer))["t"] == "ready"
        await _send(writer, {"t": "from-a-newer-client", "anything": 1})
        await _send(writer, {"t": "sessions"})
        reply = await _recv_skipping(reader, "notice")
        assert reply["t"] == "sessions"
        assert reply["sessions"][0]["id"] == "s1"
    finally:
        await _close(writer, server)


async def test_non_json_line_is_skipped_not_fatal() -> None:
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        assert (await _handshake(reader, writer))["t"] == "ready"
        writer.write(b"{ this is not json\n")
        await writer.drain()
        await _send(writer, {"t": "sessions"})
        reply = await _recv_skipping(reader, "notice")
        assert reply["t"] == "sessions"
    finally:
        await _close(writer, server)


async def test_prompt_streams_events_over_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt reaches the client as ``ev`` frames plus one turn_done."""

    async def stream(text: str, *_args: object, **_kwargs: object) -> Any:  # noqa: ANN401
        assert text == "hello"
        yield ev.AssistantMessage("hi there", "Nova", "cyan")
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        assert (await _handshake(reader, writer))["t"] == "ready"
        await _send(writer, {"t": "prompt", "id": "p1", "text": "hello"})
        message = await _recv_skipping(reader, "notice")
        assert message["t"] == "ev"
        assert message["c"] == "AssistantMessage"
        assert message["d"]["text"] == "hi there"
        done = await _recv_skipping(reader, "notice", "ev")
        assert done == {"t": "turn_done", "id": "p1", "ok": True}
    finally:
        await _close(writer, server)


async def test_handshake_timeout_closes_an_idle_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "HANDSHAKE_TIMEOUT", 0.05)
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        error = await _recv(reader)
        assert error["t"] == "error"
        assert error["code"] == "handshake"
        assert await asyncio.wait_for(reader.readline(), timeout=5) == b""
    finally:
        await _close(writer, server)


async def test_port_is_released_when_the_server_stops() -> None:
    server, port = await _serve()
    await server.stop()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_stop_closes_a_live_connection() -> None:
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        assert (await _handshake(reader, writer))["t"] == "ready"
        await server.stop()
        assert await asyncio.wait_for(reader.readline(), timeout=5) == b""
    finally:
        writer.close()
        await writer.wait_closed()


async def test_a_client_that_dies_mid_session_leaves_the_server_serving() -> None:
    server, port = await _serve()
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    reader, writer = await _connect(port)
    try:
        assert (await _handshake(reader, writer))["t"] == "ready"
        # Abort, not close: the user hit Ctrl-C or the terminal vanished. This
        # is the reset Windows' proactor reports from its IOCP callback, which
        # is why it used to escape as "Unhandled exception in client_connected_cb"
        # long after the awaiting coroutine had finished.
        writer.transport.abort()
        await asyncio.sleep(0.2)
        assert not errors, f"a dead client logged errors: {errors!r}"
        # The listener is still the same process: the next client gets a session.
        reader2, writer2 = await _connect(port)
        try:
            assert (await _handshake(reader2, writer2))["t"] == "ready"
        finally:
            writer2.close()
            await writer2.wait_closed()
    finally:
        loop.set_exception_handler(previous)
        del reader
        await server.stop()


async def test_a_command_frame_comes_back_as_a_command_result() -> None:
    """/status over the wire is the same path the Rust client's slash uses."""
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        assert (await _handshake(reader, writer))["t"] == "ready"
        await _send(writer, {"t": "command", "id": "c1", "name": "status"})
        result = await _recv_skipping(reader, "notice", "ev")
        assert result["t"] == "command_result"
        assert result["id"] == "c1"
        assert result["ok"] is True
        assert result["blocks"], "a command result must carry rendered output"
    finally:
        await _close(writer, server)


class _FakeChildHost:
    """A child host with the transport surface, but no process."""

    def __init__(self, session_id: str, registry: Any) -> None:  # noqa: ANN401 - test double
        self.session_id = session_id
        self._registry = registry
        self._sinks: list[Any] = []
        self.closed = False

    def row(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "name": "child",
            "cwd": "C:/work/child",
            "status": "idle",
            "model": "test-model",
        }

    def add_sink(self, sink: Any) -> None:  # noqa: ANN401 - test double
        self._sinks.append(sink)

    def remove_sink(self, sink: Any) -> None:  # noqa: ANN401 - test double
        self._sinks.remove(sink)

    async def list_sessions(self) -> None:
        frame = frames.sessions_frame(self._registry.summary(), active=self.session_id)
        for sink in self._sinks:
            sink(frame)

    async def close(self) -> None:
        self.closed = True


class _FakeChildManager:
    """Stand-in for `ChildSessionManager`: registers hosts, never spawns."""

    # Every manager a transport builds, so a test can inspect what reached it.
    instances: ClassVar[list[Any]] = []

    def __init__(self, registry: Any, _base: Any = None) -> None:  # noqa: ANN401 - test double
        self._registry = registry
        self.hosts: dict[str, _FakeChildHost] = {}
        self.requests: list[dict[str, Any]] = []
        self.instances.append(self)

    async def create(
        self,
        name: str,
        cwd: Any = None,  # noqa: ANN401 - test double
        *,
        resume_id: str | None = None,
    ) -> _FakeChildHost:
        self.requests.append({"name": name, "cwd": cwd, "resume_id": resume_id})
        host = _FakeChildHost(f"child-{len(self.hosts) + 1}", self._registry)
        self.hosts[host.session_id] = host
        self._registry.add(host)
        return host

    async def close(self, session_id: str) -> bool:
        host = self.hosts.pop(session_id, None)
        if host is None:
            return False
        self._registry.remove(host)
        await host.close()
        return True

    async def close_all(self) -> None:
        for session_id in list(self.hosts):
            await self.close(session_id)


async def test_create_session_spawns_attaches_and_close_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "ChildSessionManager", _FakeChildManager)
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        ready = await _handshake(reader, writer)
        parent = ready["session_id"]
        await _send(writer, frames.create_session(name="docs"))
        opened = await _recv_skipping(reader, "notice")
        assert opened["t"] == "sessions"
        assert opened["active"] == "child-1"
        assert {row["id"] for row in opened["sessions"]} == {parent, "child-1"}

        # A resume request is forwarded with the session id it continues.
        await _send(writer, frames.create_session(name="back", resume="saved-1"))
        resumed = await _recv_skipping(reader, "notice")
        assert resumed["active"] == "child-2"
        assert _FakeChildManager.instances[-1].requests[-1] == {
            "name": "back",
            "cwd": None,
            "resume_id": "saved-1",
        }

        await _send(writer, frames.close_session("child-2"))
        await _recv_skipping(reader, "notice")
        await _send(writer, frames.close_session("child-1"))
        closed = await _recv_skipping(reader, "notice")
        assert closed["t"] == "sessions"
        assert closed["active"] == parent
        assert [row["id"] for row in closed["sessions"]] == [parent]
    finally:
        await _close(writer, server)


async def test_close_refuses_a_session_this_connection_did_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "ChildSessionManager", _FakeChildManager)
    server, port = await _serve()
    reader, writer = await _connect(port)
    try:
        await _handshake(reader, writer)
        await _send(writer, frames.close_session("someone-else"))
        notice = await _recv(reader)
        assert notice["t"] == "notice"
        assert notice["level"] == "warning"
        assert "started" in notice["message"]
    finally:
        await _close(writer, server)
