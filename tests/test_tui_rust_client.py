"""Cross-language check: the Rust client really speaks the Python protocol.

Skipped when ``rust/target/debug/nova-tui`` has not been built (``make rust``),
so a machine without a Rust toolchain still runs the suite. When it *is* built,
this is the only test that puts the real decoder in front of the real server:
the binary is pointed at a live stub session and must complete
``hello`` → ``auth`` → ``ready`` before it exits.

The client's own terminal setup is expected to fail here (a test runner has no
TTY); the assertion is about the handshake, not about drawing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from novacode_cli.server.host import SessionHost, SessionSpec
from novacode_cli.server.transport import TuiServer

ROOT = Path(__file__).resolve().parents[1]
BINARY_NAME = "nova-tui.exe" if __import__("sys").platform == "win32" else "nova-tui"
BINARY = ROOT / "rust" / "target" / "debug" / BINARY_NAME
TOKEN = "cross-language-token"  # noqa: S105 — test fixture, not a credential


class _SS:
    """Minimal session-state stand-in."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.todos = None
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None


@pytest.mark.skipif(not BINARY.is_file(), reason="nova-tui binary not built (run: make rust)")
async def test_the_rust_client_completes_the_handshake() -> None:
    started: asyncio.Event = asyncio.Event()

    async def host_factory(emit: Any) -> SessionHost:  # noqa: ANN401 — the transport's sink
        def spec() -> SessionSpec:
            started.set()
            return SessionSpec(
                agent=object(),
                session_state=_SS(),
                assistant_id="nova-agent",
                model_name="integration-model",
                cwd="C:/work",
                session_id="s1",
            )

        return SessionHost(emit, spec)

    server = TuiServer(host_factory, token=TOKEN)
    port = await server.start()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(BINARY),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            TOKEN,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(started.wait(), timeout=30)
        assert started.is_set(), "the client never authenticated"
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        await server.stop()


@pytest.mark.skipif(not BINARY.is_file(), reason="nova-tui binary not built (run: make rust)")
async def test_the_rust_client_asks_for_the_command_catalogue() -> None:
    """The real binary asks for the catalogue, and the server answers it.

    The client sends @@catalogue@@ right after the handshake - before it touches
    the terminal - so this runs headless and still exercises the new envelope in
    both languages: Rust encodes it, Python decodes it, Python encodes the reply,
    Rust would decode it.
    """
    frames: list[dict[str, Any]] = []
    asked: asyncio.Event = asyncio.Event()

    async def host_factory(emit: Any) -> SessionHost:  # noqa: ANN401 — the transport's sink
        def spec() -> SessionSpec:
            return SessionSpec(
                agent=object(),
                session_state=_SS(),
                assistant_id="nova-agent",
                model_name="integration-model",
                cwd="C:/work",
                session_id="s1",
            )

        def record(frame: dict[str, Any]) -> None:
            frames.append(frame)
            if frame.get("t") == "catalogue":
                asked.set()
            emit(frame)

        return SessionHost(record, spec)

    server = TuiServer(host_factory, token=TOKEN)
    port = await server.start()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(BINARY),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            TOKEN,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(asked.wait(), timeout=30)
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        await server.stop()

    catalogue = [frame for frame in frames if frame.get("t") == "catalogue"][-1]
    names = [entry["name"] for entry in catalogue["commands"]]
    from novacode_cli.server.catalog import CATALOG

    assert set(names) == set(CATALOG)
    assert "help" in names
    entry = catalogue["commands"][names.index("help")]
    assert entry["served"] is True
    assert entry["aliases"] == ["?"]


@pytest.mark.skipif(not BINARY.is_file(), reason="nova-tui binary not built (run: make rust)")
def test_the_rust_client_reports_a_bad_endpoint() -> None:
    completed = subprocess.run(  # noqa: S603 — fixed path to our own binary
        [str(BINARY), "--host", "127.0.0.1", "--port", "1", "--token", TOKEN],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    assert "cannot connect" in completed.stderr


@pytest.mark.skipif(not BINARY.is_file(), reason="nova-tui binary not built (run: make rust)")
async def test_the_rust_client_round_trips_help_and_a_screen() -> None:
    """The real binary runs `/help` and answers a `screen` frame.

    A test runner has no TTY, so the interactive loop cannot run; `--probe` drives
    the same decoders and encoders instead. The server side then asserts that
    the answer it received was the documented default for that screen kind, so
    the round trip is cross-language in both directions.
    """
    from novacode_cli.ui.port import ScreenSpec

    seen: list[dict[str, Any]] = []
    hosts: list[SessionHost] = []
    started: asyncio.Event = asyncio.Event()

    async def host_factory(emit: Any) -> SessionHost:  # noqa: ANN401 — the transport's sink
        def spec() -> SessionSpec:
            started.set()
            return SessionSpec(
                agent=object(),
                session_state=_SS(),
                assistant_id="nova-agent",
                model_name="integration-model",
                cwd="C:/work",
                session_id="s1",
            )

        def record(frame: dict[str, Any]) -> None:
            seen.append(frame)
            emit(frame)

        host = SessionHost(record, spec)
        hosts.append(host)
        return host

    server = TuiServer(host_factory, token=TOKEN)
    port = await server.start()
    process = None
    asking: asyncio.Task[Any] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(BINARY),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            TOKEN,
            "--probe",
            "/help",
            "--probe-grace-ms",
            "1500",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(started.wait(), timeout=30)
        # Ask for a modal while the probe is still listening, so the client's
        # answer is what resolves this future.
        asking = asyncio.create_task(
            hosts[0]._ask_screen(ScreenSpec("confirm", {"message": "go?"}, "Confirm"))
        )
        answer = await asyncio.wait_for(asking, timeout=30)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        await server.stop()

    assert answer is False, "the probe answers a confirm with the documented default"
    assert process.returncode == 0, stderr.decode()
    lines = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith("{")]
    kinds = [line.get("t") for line in lines]
    assert "catalogue" in kinds
    result = [line for line in lines if line.get("t") == "command_result"][-1]
    assert result["ok"] is True
    assert result["blocks"], "a served command must return its output as blocks"
    screen = [line for line in lines if line.get("t") == "screen"][-1]
    assert screen["kind"] == "confirm"
    assert screen["title"] == "Confirm"
    # The client's answer is not in its own stdout (that is inbound only): the
    # proof it answered is the server-side future resolving above.
