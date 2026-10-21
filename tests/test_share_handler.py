"""Temporary shares use flags supported by the current ngrok agent CLI."""

from __future__ import annotations

import io

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
        assert manager.start("artifacts", 43123) == "https://nova.ngrok-free.app"
    finally:
        manager.stop_all()

    args = captured["args"]
    assert isinstance(args, list)
    assert "--inspect=false" in args
    assert not any(arg.startswith("--web-addr") for arg in args)
