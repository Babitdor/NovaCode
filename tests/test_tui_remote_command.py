"""`/remote` in the socket port.

The bridge surface already lives in the shared handler, so the port delegates and
returns its wording. What this pins: bare `/remote` asks for status instead of
trying to open a Textual screen, arguments pass through untouched, and the
handler's printed output becomes blocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from novacode_cli.server.catalog import CATALOG
from novacode_cli.ui.port_server import ServerPort


def _port() -> ServerPort:
    """A port over one session, emitting nowhere."""
    worker = SimpleNamespace(
        agent=object(),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
        token_tracker=None,
        session_state=SimpleNamespace(thread_id="t1", session_id="s1"),
    )
    return ServerPort(lambda _frame: None, worker)


async def test_bare_remote_asks_for_the_status(monkeypatch: Any) -> None:  # noqa: ANN401
    seen: list[str] = []

    async def handler(args: str | None, _state: object, _console: object) -> bool:
        seen.append(args or "")
        print("bridges: none configured")  # noqa: T201 — the handler prints its own wording
        return True

    monkeypatch.setattr("novacode_cli.commands.commands._handle_remote_command", handler)

    result = await _port()._remote_command(None)

    assert seen == ["status"]
    assert result.ok is True
    assert any("bridges: none configured" in block.text for block in result.blocks)


async def test_arguments_reach_the_handler_unchanged(monkeypatch: Any) -> None:  # noqa: ANN401
    seen: list[str] = []

    async def handler(args: str | None, _state: object, _console: object) -> bool:
        seen.append(args or "")
        return True

    monkeypatch.setattr("novacode_cli.commands.commands._handle_remote_command", handler)

    await _port()._remote_command("stop telegram")

    assert seen == ["stop telegram"]


def test_the_catalogue_serves_the_remote() -> None:
    assert CATALOG["remote"].served is True
