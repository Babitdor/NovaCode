"""Temporary shares use flags supported by the current ngrok agent CLI."""

from __future__ import annotations

import asyncio
import io
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
        assert manager.start("artifacts", 43123) == "https://nova.ngrok-free.app"
    finally:
        manager.stop_all()

    args = captured["args"]
    assert isinstance(args, list)
    assert "--inspect=false" in args
    assert not any(arg.startswith("--web-addr") for arg in args)


def test_dashboard_lists_all_pages_and_keeps_unshared_pages_clear() -> None:
    page = share_handler._dashboard_html(
        {
            "artifacts": "https://artifacts.example/",
            "council": None,
            "cowork": "https://cowork.example/?token=a&b=c",
            "create": None,
        }
    )

    for title in ("Artifacts", "Council", "Cowork", "Create"):
        assert title in page
    assert "https://artifacts.example/" in page
    assert "https://cowork.example/?token=a&amp;b=c" in page
    assert page.count("Not shared yet") == 2
    assert "@media(max-width:620px)" in page
    assert 'rel="noopener"' in page


def test_share_all_starts_each_page_and_publishes_dashboard(monkeypatch) -> None:
    started: list[str] = []

    async def start(service, _ctx):
        started.append(service)
        return f"https://{service}.example/"

    monkeypatch.setattr(share_handler, "_start_share", start)
    monkeypatch.setattr(
        share_handler, "_publish_dashboard", lambda: "https://artifacts.example/dashboard"
    )
    ctx = SimpleNamespace(cmd_args="all")

    assert asyncio.run(share_handler.handle_share_command(ctx)) is True
    assert started == ["artifacts", "council", "cowork", "create"]
