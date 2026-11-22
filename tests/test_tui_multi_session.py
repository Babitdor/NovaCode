"""More than one session on one server: listing, attaching, capacity.

Two real loopback clients, each with its own stand-in session. Before this
wave a connection could only ever see the session its own factory built; the
registry is what makes `sessions` a list and `attach` a real switch.

No model and no subprocess: hosts are stand-ins, as in
`tests/test_tui_server_transport.py`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from novacode_cli.server import frames
from novacode_cli.server.host import SessionHost, SessionSpec
from novacode_cli.server.sessions import SessionCapacityError, SessionRegistry
from novacode_cli.server.transport import TuiServer

if TYPE_CHECKING:
    from collections.abc import Callable

TEST_SECRET = "test-token-0123456789"  # noqa: S105


class _SS:
    """Session-state stand-in with an id under our control."""

    def __init__(self, session_id: str) -> None:
        self.thread_id = f"{session_id}-thread"
        self.session_id = session_id
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos = None
        self.steering_instructions = []
        self.headless_out_fd = None


def _spec(session_id: str, *, name: str = "work") -> SessionSpec:
    """Build a session spec that needs no agent."""
    return SessionSpec(
        agent=object(),
        session_state=_SS(session_id),
        assistant_id="nova-agent",
        model_name=f"model-{session_id}",
        cwd=f"C:/work/{session_id}",
        name=name,
    )


async def _send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """Write one frame."""
    writer.write(frames.encode_line(msg))
    await writer.drain()


async def _recv(reader: asyncio.StreamReader, *, deadline: float = 5.0) -> dict[str, Any]:
    """Read the next frame, failing on EOF or garbage."""
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


async def _join(
    port: int, session_id: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect, handshake, and return the socket once its session is ready."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await _send(writer, frames.hello(role="client", name="nova-tui/test"))
    await _recv(reader)
    await _send(writer, {"t": "auth", "token": TEST_SECRET})
    ready = await _recv_skipping(reader, "notice")
    assert ready["t"] == "ready"
    assert ready["session_id"] == session_id
    return reader, writer


async def _serve(sessions: list[str], *, capacity: int = 4) -> tuple[TuiServer, int]:
    """Start a server that hands out one stand-in session per connection."""
    queue = list(sessions)

    async def host_factory(emit: Callable[[dict[str, Any]], None]) -> SessionHost:
        session_id = queue.pop(0) if queue else f"s{len(sessions) + 1}"
        return SessionHost(emit, lambda: _spec(session_id))

    server = TuiServer(
        host_factory,
        token=TEST_SECRET,
        registry=SessionRegistry(capacity=capacity),
    )
    port = await server.start()
    return server, port


async def test_two_connections_are_two_sessions() -> None:
    server, port = await _serve(["alpha", "beta"])
    writer_a: asyncio.StreamWriter | None = None
    writer_b: asyncio.StreamWriter | None = None
    try:
        reader_a, writer_a = await _join(port, "alpha")
        reader_b, writer_b = await _join(port, "beta")

        await _send(writer_a, {"t": "sessions"})
        listed = await _recv(reader_a)
        assert listed["t"] == "sessions"
        assert [row["id"] for row in listed["sessions"]] == ["alpha", "beta"]
        assert listed["active"] == "alpha"
        assert listed["sessions"][0]["model"] == "model-alpha"
        assert listed["sessions"][1]["status"] == "idle"

        # The other connection is unaffected by what the first one asked.
        await _send(writer_b, {"t": "sessions"})
        other = await _recv(reader_b)
        assert other["active"] == "beta"
    finally:
        for writer in (writer_a, writer_b):
            if writer is not None:
                writer.close()
                await writer.wait_closed()
        await server.stop()


async def test_attach_switches_the_connection_to_another_session() -> None:
    server, port = await _serve(["alpha", "beta"])
    writer_a: asyncio.StreamWriter | None = None
    writer_b: asyncio.StreamWriter | None = None
    try:
        reader_a, writer_a = await _join(port, "alpha")
        _, writer_b = await _join(port, "beta")

        await _send(writer_a, {"t": "attach", "session_id": "beta"})
        notice = await _recv(reader_a)
        assert notice["t"] == "notice"
        assert "beta" in notice["message"]
        listed = await _recv(reader_a)
        assert listed["t"] == "sessions"
        assert listed["active"] == "beta"

        # Attaching again is idempotent and says so.
        await _send(writer_a, {"t": "attach", "session_id": "beta"})
        again = await _recv(reader_a)
        assert again["t"] == "notice"
        assert "Already attached" in again["message"]

        # An unknown id keeps the current session rather than dropping it.
        await _send(writer_a, {"t": "attach", "session_id": "nope"})
        unknown = await _recv(reader_a)
        assert unknown["t"] == "notice"
        await _send(writer_a, {"t": "sessions"})
        still = await _recv(reader_a)
        assert still["active"] == "beta"
    finally:
        for writer in (writer_a, writer_b):
            if writer is not None:
                writer.close()
                await writer.wait_closed()
        await server.stop()


async def test_a_closing_client_leaves_the_attached_session_alive() -> None:
    server, port = await _serve(["alpha", "beta"])
    writer_b: asyncio.StreamWriter | None = None
    try:
        reader_a, writer_a = await _join(port, "alpha")
        reader_b, writer_b = await _join(port, "beta")
        await _send(writer_a, {"t": "attach", "session_id": "beta"})
        assert (await _recv(reader_a))["t"] == "notice"
        attached = await _recv(reader_a)
        assert attached["t"] == "sessions"
        # The target's frames now fan out to both viewers, so B sees the same
        # list A does: that is what "attached" means.
        seen_by_b = await _recv(reader_b)
        assert seen_by_b["t"] == "sessions"
        assert seen_by_b["active"] == "beta"

        # Client A owns "alpha" but is attached to "beta": closing A must not
        # close B's session.
        writer_a.close()
        await writer_a.wait_closed()
        await asyncio.sleep(0.15)

        await _send(writer_b, {"t": "sessions"})
        listed = await _recv(reader_b)
        ids = [row["id"] for row in listed["sessions"]]
        assert "beta" in ids
        assert "alpha" not in ids, "a closed session must leave the registry"
    finally:
        if writer_b is not None:
            writer_b.close()
            await writer_b.wait_closed()
        await server.stop()


async def test_the_registry_refuses_a_session_past_capacity() -> None:
    server, port = await _serve(["one", "two"], capacity=2)
    writers: list[asyncio.StreamWriter] = []
    try:
        _, first = await _join(port, "one")
        writers.append(first)
        _, second = await _join(port, "two")
        writers.append(second)

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(writer)
        await _send(writer, frames.hello(role="client", name="nova-tui/test"))
        await _recv(reader)
        await _send(writer, {"t": "auth", "token": TEST_SECRET})
        error = await _recv_skipping(reader, "notice")
        assert error["t"] == "error"
        assert error["code"] == "capacity"
        assert await asyncio.wait_for(reader.readline(), timeout=5) == b""
    finally:
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        await server.stop()


def test_the_registry_tracks_ids_and_capacity() -> None:
    registry = SessionRegistry(capacity=1)

    class _Host:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id

        def row(self) -> dict[str, Any]:
            return {"id": self.session_id, "name": "", "cwd": None, "status": "idle", "model": None}

    first = _Host("a")
    registry.add(first)  # type: ignore[arg-type]
    assert len(registry) == 1
    assert "a" in registry
    assert registry.get("a") is first
    assert not registry.has_room()
    with pytest.raises(SessionCapacityError):
        registry.add(_Host("b"))  # type: ignore[arg-type]
    registry.remove(first)  # type: ignore[arg-type]
    assert len(registry) == 0
    assert registry.summary() == []
