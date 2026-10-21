"""Temporary shares use flags supported by the current ngrok agent CLI."""

from __future__ import annotations

import asyncio
import io
import socket
from types import SimpleNamespace

from novacode_cli.commands import share_handler
from novacode_cli.commands.share_handler import ShareManager


def test_ngrok_startup_avoids_removed_web_addr_flag(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = io.StringIO(
            '{"msg":"started tunnel","url":"https://nova.ngrok-free.app"}\n'
        )

        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("novacode_cli.commands.share_handler.shutil.which", lambda _: "ngrok")
    monkeypatch.setattr("novacode_cli.commands.share_handler.subprocess.Popen", FakeProcess)
    manager = ShareManager()

    try:
        assert manager.start("council", 43123) == "https://nova.ngrok-free.app"
    finally:
        manager.stop_all()

    args = captured["args"]
    assert isinstance(args, list)
    assert "--inspect=false" in args
    assert not any(arg.startswith("--web-addr") for arg in args)


def test_artifacts_prefer_cloudflare_quick_tunnels(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = io.StringIO(
            "Quick Tunnel created: https://quiet-moon.trycloudflare.com\n"
        )

        def __init__(self, args, **kwargs):
            captured["args"] = args

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        "novacode_cli.commands.share_handler.shutil.which",
        lambda name: "cloudflared.exe" if name == "cloudflared" else None,
    )
    monkeypatch.setattr("novacode_cli.commands.share_handler.subprocess.Popen", FakeProcess)
    manager = ShareManager()

    try:
        assert manager.start("artifacts", 43123) == "https://quiet-moon.trycloudflare.com"
    finally:
        manager.stop_all()

    assert captured["args"] == [
        "cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:43123"
    ]


def test_share_all_only_starts_artifacts(monkeypatch) -> None:
    started: list[str] = []

    async def start(service, _ctx):
        started.append(service)
        return f"https://{service}.example/"

    monkeypatch.setattr(share_handler, "_start_share", start)
    ctx = SimpleNamespace(cmd_args="all")

    assert asyncio.run(share_handler.handle_share_command(ctx)) is True
    assert started == ["artifacts"]


def test_upstream_readiness_check_accepts_a_listening_local_page() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        share_handler._wait_for_upstream(listener.getsockname()[1], timeout=0.2)


def test_upstream_readiness_check_reports_a_closed_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    try:
        share_handler._wait_for_upstream(port, timeout=0.2)
    except RuntimeError as exc:
        assert f"127.0.0.1:{port}" in str(exc)
    else:
        raise AssertionError("a closed local port must not be published")


def test_stop_session_revokes_only_owned_shares_and_honors_exclusions() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

        def terminate(self) -> None:
            self.stopped = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

    manager = ShareManager()
    artifact = FakeProcess()
    council = FakeProcess()
    other_session = FakeProcess()
    manager._tunnels = {
        "artifacts": share_handler._Tunnel(
            "artifacts", 1, artifact, "https://a.ngrok.app", session_id="old"
        ),
        "council": share_handler._Tunnel(
            "council", 2, council, "https://c.ngrok.app", session_id="old"
        ),
        "create": share_handler._Tunnel(
            "create", 3, other_session, "https://x.ngrok.app", session_id="new"
        ),
    }

    assert manager.stop_session("old", exclude=frozenset({"artifacts"})) == {"council"}
    assert not artifact.stopped
    assert council.stopped
    assert not other_session.stopped
    assert manager.list() == {"artifacts": "https://a.ngrok.app", "create": "https://x.ngrok.app"}


def test_artifact_share_records_session_owner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(share_handler, "_wait_for_upstream", lambda _port: None)

    def start(service, port, *, server=None, session_id=None):
        captured.update(service=service, port=port, session_id=session_id)
        return "https://share.ngrok.app"

    monkeypatch.setattr(share_handler._manager, "start", start)
    monkeypatch.setattr(share_handler._manager, "set_public_url", lambda service, url: None)

    url = share_handler.ensure_share_for_url(
        "artifacts", "http://127.0.0.1:43123/", session_id="session-1"
    )

    assert url == "https://share.ngrok.app/"
    assert captured == {"service": "artifacts", "port": 43123, "session_id": "session-1"}


def test_share_command_rejects_council_cowork_and_create(monkeypatch) -> None:
    started: list[str] = []

    async def start(service: str, _ctx) -> str:
        started.append(service)
        return "https://example.test"

    monkeypatch.setattr(share_handler, "_start_share", start)

    async def run(service: str) -> None:
        ctx = SimpleNamespace(cmd_args=service)
        assert await share_handler.handle_share_command(ctx) is True

    for service in ("council", "cowork", "create"):
        asyncio.run(run(service))

    assert started == []
