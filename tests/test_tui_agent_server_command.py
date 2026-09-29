# ruff: noqa: ANN202, ANN401, ARG001, ARG005 — stubs and fakes mirror the
# signatures they stand in for, so unused or dynamic parameters are the point.
"""The /agent-server command, driven through the real app.

This is the only way to see why a dynamic subagent that needed a server fell back
to the in-process path, so every branch has to run without raising. The first
version of it did raise: it called the footer palette instead of reading it
(``self._palette()`` where ``_palette`` is a property), which a grep over the
source could not catch and a test that runs the handler does.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="deepseek-v4.1-flash",
    )


async def _drive(command: str, *, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run ``/agent-server <command>`` and return what it wrote to the transcript."""
    written: list[str] = []

    app = _app()
    app._log = lambda *args, **kwargs: written.append(str(args[0] if args else ""))  # type: ignore[method-assign]

    async with app.run_test(size=(120, 44)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await app._run_agent_server(command)
        await pilot.pause()
    return "\n".join(written)


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_status_reports_the_server_and_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default action. Reading the palette must not call it."""
    import asyncio

    text = asyncio.run(_drive("", monkeypatch=monkeypatch))

    assert "agent server" in text
    assert "local server" in text
    assert "async agents" in text
    assert "extra" in text
    assert "uv sync --extra agents-server" in text  # the hint when it is missing


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_logs_says_so_when_nothing_has_been_launched(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from novacode_cli.agents import server_launcher

    monkeypatch.setattr(
        server_launcher,
        "agent_server_status",
        lambda: {
            "running": False,
            "url": None,
            "log_path": None,
            "graphs": [],
        },
    )
    text = asyncio.run(_drive("logs", monkeypatch=monkeypatch))

    assert "no agent server log" in text


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_stop_is_safe_when_nothing_was_started(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    text = asyncio.run(_drive("stop", monkeypatch=monkeypatch))

    assert "stopped" in text


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_start_reports_when_it_cannot_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No extra installed and nothing answering: it must say so, not raise."""
    import asyncio

    from novacode_cli.agents import server_launcher

    async def no_server(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(server_launcher, "ensure_agent_server", no_server)
    text = asyncio.run(_drive("start", monkeypatch=monkeypatch))

    assert "not started" in text


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_start_reports_the_url_when_it_did_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from novacode_cli.agents import server_launcher

    class _Started:
        url = "http://127.0.0.1:2166"
        log_path = None
        graph_names: ClassVar[list[str]] = []

        @property
        def running(self) -> bool:
            return True

    async def launch(**kwargs: Any) -> Any:
        return _Started()

    monkeypatch.setattr(server_launcher, "ensure_agent_server", launch)
    text = asyncio.run(_drive("start", monkeypatch=monkeypatch))

    assert "running at http://127.0.0.1:2166" in text


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_an_unknown_action_prints_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    text = asyncio.run(_drive("wibble", monkeypatch=monkeypatch))

    assert "usage: /agent-server" in text


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_the_command_is_registered_without_taking_agents() -> None:
    """`/agents` lists subagents and must keep its handler."""
    from novacode_cli.tui.app import TUI_COMMANDS

    assert TUI_COMMANDS["agent-server"].handler == "_run_agent_server"
    assert TUI_COMMANDS["agents"].handler == "_run_agents"
