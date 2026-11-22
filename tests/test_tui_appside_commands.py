"""App-side commands now served by the port: tokens, copy, steer, redirect, nova."""

from __future__ import annotations

import types
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from novacode_cli.ui.port_server import ServerPort


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        agent=object(),
        session_state=SimpleNamespace(
            thread_id="t1",
            session_id="s1",
            auto_approve=False,
            todos=[],
            headless_out_fd=None,
            steering_instructions=[],
            token_tracker=SimpleNamespace(
                current_context=1_000,
                context_window_size=200_000,
                session_total_tokens=5_000,
                last_output=321,
                last_cache_read=10,
                last_cache_creation=20,
                has_api_data=True,
            ),
        ),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
        token_tracker=None,
    )


def _port(answers: list[object] | None = None) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []
    queue = list(answers or [])

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    sent: list[dict[str, Any]] = []
    port = ServerPort(sent.append, _worker(), ask_screen=ask_screen)
    port._sent = sent  # type: ignore[attr-defined]
    return port, asked


async def test_tokens_reports_session_and_context() -> None:
    port, _ = _port()
    result = await port.run("/tokens", None)
    assert result.ok is True
    assert result.screen is not None, "tokens must open its detail screen"
    assert result.screen.kind == "detail"
    markdown = result.screen.payload["markdown"]
    assert "1,000 of 200,000" in markdown
    assert "5,000" in markdown


async def test_cost_shares_the_tokens_path() -> None:
    port, _ = _port()
    result = await port.run("/cost", None)
    assert result.ok is True
    assert result.screen is not None, "cost must open its detail screen"
    assert result.screen.kind == "detail"


async def test_a_missing_tracker_is_the_empty_state() -> None:
    port, _ = _port()
    port._worker.session_state.token_tracker = None
    port._worker.token_tracker = None
    result = await port.run("/tokens", None)
    assert result.ok is True
    assert result.screen is not None
    assert "No token usage" in result.screen.payload["markdown"]


async def test_steer_adds_lists_and_clears() -> None:
    port, _ = _port()
    added = await port.run("/steer", "always run the tests before reporting")
    assert added.ok is True
    assert "added" in added.blocks[0].text.lower()
    listed = await port.run("/steer", None)
    assert "always run the tests" in listed.blocks[0].text
    cleared = await port.run("/steer", "clear")
    assert "cleared 1" in cleared.blocks[0].text.lower()
    assert port._worker.session_state.steering_instructions == []


async def test_steer_remove_takes_a_number() -> None:
    port, _ = _port()
    await port.run("/steer", "never push to main")
    removed = await port.run("/steer", "remove 1")
    assert removed.ok is True
    assert "removed" in removed.blocks[0].text.lower()
    assert port._worker.session_state.steering_instructions == []


async def test_redirect_names_a_cancel_and_a_prompt() -> None:
    port, _ = _port()
    result = await port.run("/redirect", "run the linter instead")
    assert result.ok is True
    assert result.action == "cancel"
    assert result.prompt == "run the linter instead"
    usage = await port.run("/redirect", None)
    assert usage.ok is False


async def test_copy_names_the_client_action() -> None:
    port, _ = _port()
    last = await port.run("/copy", None)
    assert last.action == "copy"
    everything = await port.run("/copy", "all")
    assert everything.action == "copy-all"


async def test_nova_is_an_avatar_toggle() -> None:
    port, _ = _port()
    result = await port.run("/nova", None)
    assert result.ok is True
    assert result.action == "avatar"


async def test_export_writes_the_session_html(tmp_path: Path) -> None:
    from novacode_cli.session.session_persistence import SessionManager

    port, _ = _port()
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    port._worker.session_manager = manager
    port._worker.session_state.session_id = "export-test"
    target = tmp_path / "out" / "session.html"
    result = await port.run("/export", str(target))
    assert result.ok is True, result.message
    assert target.exists()
    assert "Nova session" in target.read_text(encoding="utf-8")


async def test_export_without_persistence_is_explicit() -> None:
    port, _ = _port()
    result = await port.run("/export", None)
    assert result.ok is False
    assert "history is off" in result.message.lower()


async def test_worktree_needs_a_git_repository(tmp_path: Path) -> None:
    port, _ = _port()
    port._cwd = str(tmp_path)
    result = await port.run("/worktree", None)
    assert result.ok is False
    assert "git repository" in result.message.lower()


def _patch_agent_server(monkeypatch: pytest.MonkeyPatch, *, running: bool) -> None:
    """Patch the launcher and the lazy-imported subagent helpers."""
    import sys

    from novacode_cli.agents import server_launcher

    def fake_status() -> dict:
        return {
            "running": running,
            "url": "http://127.0.0.1:2024" if running else "",
            "log_path": "",
            "graphs": ["x"] if running else [],
        }

    monkeypatch.setattr(server_launcher, "agent_server_status", fake_status)
    monkeypatch.setattr(server_launcher, "server_extra_available", lambda: True)

    module = sys.modules.setdefault(
        "novacode_cli.agents.default_subagents.async_subagents",
        types.SimpleNamespace(
            async_agents_available=lambda **_: True,
            async_agents_see_workspace=lambda: True,
        ),
    )
    monkeypatch.setattr(module, "async_agents_available", lambda **_: True)
    monkeypatch.setattr(module, "async_agents_see_workspace", lambda: True)


async def test_agent_server_status_reports_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_agent_server(monkeypatch, running=True)
    port, _ = _port()
    result = await port.run("/agent-server", None)
    assert result.ok is True
    text = result.blocks[0].text
    assert "running" in text
    assert "2024" in text


async def test_agent_server_usage_is_refused() -> None:
    port, _ = _port()
    result = await port.run("/agent-server", "bogus")
    assert result.ok is False
    assert "usage" in result.message.lower()


async def test_btw_refuses_an_empty_question() -> None:
    port, _ = _port()
    result = await port.run("/btw", None)
    assert result.ok is False
    assert "usage" in result.message.lower()


async def test_btw_reports_a_failed_agent_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novacode_cli.commands.side_commands as side

    def broken() -> object:
        message = "no provider"
        raise RuntimeError(message)

    monkeypatch.setattr(side, "get_btw_agent", broken)
    port, _ = _port()
    result = await port.run("/btw", "what is up")
    assert result.ok is False
    assert "web-search agent" in result.message


async def test_btw_runs_and_logs_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    import novacode_cli.commands.side_commands as side
    from novacode_cli import agent_stream

    def fake_agent() -> object:
        return object()

    monkeypatch.setattr(side, "get_btw_agent", fake_agent)

    async def fake_stream(*_a: object, **_k: object):  # noqa: ANN202 - async generator
        from types import SimpleNamespace

        yield SimpleNamespace(text="42")

    # The command imports the function by name, so patch the module attribute.
    monkeypatch.setattr(agent_stream, "run_agent_stream", fake_stream)
    port, _ = _port()
    result = await port.run("/btw", "what is up")
    assert result.ok is True
    await port._btw_task
    # `log` sends a completed command_result; the answer must be in a block.
    blocks = [block for frame in port._sent for block in frame.get("blocks", [])]
    assert any("42" in block.get("text", "") for block in blocks)


async def test_reindex_without_semble_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import SimpleNamespace

    module = sys.modules.setdefault("novacode_cli.tools.code_search_tools", SimpleNamespace())
    monkeypatch.setattr(module, "_is_semble_available", lambda: False, raising=False)
    monkeypatch.setattr(module, "_get_index", lambda _ws: None, raising=False)
    monkeypatch.setattr(module, "_reset_index", lambda: None, raising=False)
    port, _ = _port()
    result = await port.run("/reindex", None)
    assert result.ok is False
    assert "not available" in result.message.lower() or "unavailable" in result.message.lower()
