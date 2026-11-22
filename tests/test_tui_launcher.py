"""The launcher: bind before spawn, kill on exit, fall back when missing.

The ordering guarantee is the point of this file. A client that connects before
the listener exists fails with "connection refused" and the user sees a dead UI,
so the test drives the real launch path with a fake subprocess that *connects*
while it runs. If the port were bound after the spawn, that connect would fail
here rather than in production.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from novacode_cli.server import frames
from novacode_cli.server.host import SessionSpec
from novacode_cli.ui import launcher

if TYPE_CHECKING:
    from pathlib import Path


class _SS:
    """Minimal session-state stand-in (mirrors tests/test_tui_server_transport.py)."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos = None
        self.steering_instructions = []
        self.headless_out_fd = None


def _spec() -> SessionSpec:
    """A complete spec: the launcher builds a real host, not a stand-in."""
    return SessionSpec(
        agent=object(),
        session_state=_SS(),
        assistant_id="nova-agent",
        model_name="test-model",
        cwd="C:/work",
    )


async def _recv(
    reader: asyncio.StreamReader, *, skip: str | None = None, deadline: float = 10.0
) -> dict[str, Any]:
    """Read frames until one whose type is not *skip*, failing on EOF."""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=deadline)
        assert line, "server closed the connection"
        msg = frames.decode_line(line)
        assert msg is not None, f"unparseable frame: {line!r}"
        if msg.get("t") != skip:
            return msg


class _FakeProcess:
    """Stands in for the ratatui binary: handshakes, reads ready, exits 0."""

    def __init__(self, argv: list[str], checks: dict[str, Any]) -> None:
        self.argv = argv
        self.pid = 4242
        self.returncode: int | None = None
        self.killed = False
        self._checks = checks

    async def wait(self) -> int:
        checks = self._checks
        port = int(self.argv[self.argv.index("--port") + 1])
        token = self.argv[self.argv.index("--token") + 1]
        checks["port"] = port
        checks["token"] = token
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        checks["connected"] = True
        # Connecting is not serving. A factory the transport cannot await closes
        # the socket right after auth, so the handshake has to be exercised for
        # this test to mean anything.
        writer.write(frames.encode_line(frames.hello(role="client", name="nova-tui/test")))
        await writer.drain()
        checks["hello"] = (await _recv(reader)).get("t")
        writer.write(frames.encode_line({"t": "auth", "token": token}))
        await writer.drain()
        checks["ready"] = (await _recv(reader, skip="notice")).get("t")
        writer.close()
        await writer.wait_closed()
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True


async def test_the_client_connects_before_the_launcher_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: dict[str, Any] = {}
    spec = _spec()

    async def fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        captured = list(argv)
        checks["argv"] = captured
        return _FakeProcess(captured, checks)

    monkeypatch.setattr(launcher, "resolve_binary", lambda: "/fake/nova-tui")
    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", fake_exec)

    assert await launcher.launch_ratatui(spec) is True
    assert checks.get("connected") is True, "the socket must be listening before the spawn"
    assert checks["argv"][0] == "/fake/nova-tui"
    assert checks["argv"][1] == "--host"
    assert checks["argv"][3] == "--port"
    assert len(checks["token"]) >= 32
    assert spec.session_state.headless_exit_code == 0

    # The port must be free again: otherwise the next launch cannot bind.
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", checks["port"])


async def test_a_client_that_fails_to_spawn_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "resolve_binary", lambda: "/fake/nova-tui")

    async def fake_exec(*_argv: str, **_kwargs: object) -> _FakeProcess:
        msg = "no such binary"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", fake_exec)
    assert await launcher.launch_ratatui(_spec()) is False


async def test_a_missing_binary_returns_false_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "resolve_binary", lambda: None)
    assert await launcher.launch_ratatui(_spec()) is False


def test_the_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / launcher.binary_filename()
    binary.write_bytes(b"")
    monkeypatch.setenv(launcher.BINARY_ENV, str(binary))
    assert launcher.resolve_binary() == binary


def test_candidates_are_ordered_env_package_home_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(launcher.BINARY_ENV, "/env/nova-tui")
    candidates = [str(path) for path in launcher.candidate_paths()]
    assert candidates[0].endswith("nova-tui")
    assert "env" in candidates[0]
    assert any("bin" in candidate for candidate in candidates[1:])


def test_binary_filename_matches_the_platform() -> None:
    name = launcher.binary_filename()
    assert name.startswith("nova-tui")
    if launcher.sys.platform == "win32":
        assert name.endswith(".exe")
