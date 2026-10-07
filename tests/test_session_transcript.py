"""Structured history, pagination and saved import replay."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from textual.app import App
from textual.widgets import Button, Collapsible

from novacode_cli.session.adapters import HarnessMessage, ImportedSession
from novacode_cli.session.imported_context import MESSAGE_ID, ImportedContext
from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.tui.session_import import TranscriptScreen
from novacode_cli.tui.session_transcript import SessionTranscript
from novacode_cli.tui.widgets import ChatMessage, OutputLog
from tests.test_tui_sessions import isolated_session_config  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_preview_markdown_tools_paging_and_resize(provider):
    messages = [
        HarnessMessage("user", "Fix **authentication**"),
        HarnessMessage("assistant", "## Solution\n```python\nlock.acquire()\n```"),
        HarnessMessage(
            "assistant", '{"cmd":"pytest"}', tool_name="Bash", metadata={"kind": "tool_call"}
        ),
        HarnessMessage("tool", "[red]literal output[/red]", tool_name="Bash"),
    ] + [HarnessMessage("assistant", f"Turn {n}") for n in range(40)]
    session = ImportedSession(provider, "source", "/project", messages)
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        await app.push_screen(TranscriptScreen(session))
        await pilot.pause()
        history = app.screen.query_one(SessionTranscript)
        assert len(history.query(ChatMessage)) == 18
        assert history.query(ChatMessage)[1].raw_text.startswith("## Solution")
        assert len(history.query(Collapsible)) == 2
        result = history.query(Collapsible)[1]
        assert result.collapsed
        assert "[red]literal" in result.query_one(OutputLog)._output_lines[0].plain
        result.collapsed = False
        await pilot.pause()
        assert result.query_one(OutputLog).size.height > 0
        history.query_one(".history-next", Button).scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(".history-next")
        await pilot.pause()
        assert isinstance(app.screen, TranscriptScreen)
        assert history.page == 1
        assert history.query(ChatMessage)[0].raw_text == "Turn 16"
        await pilot.resize_terminal(55, 18)
        await pilot.pause()
        assert app.screen.query_one("#import-history").size.height > 0
        history.query_one(".history-prev", Button).scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(".history-prev")
        await pilot.pause()
        assert history.page == 0
        assert history.query_one(".history-prev", Button).disabled


@pytest.mark.asyncio
async def test_saved_import_replay_hides_reference_and_survives_mode_change(tmp_path):
    from tests.test_tui_subagent_panel import _app

    app = _app()
    app.session_manager = SessionManager(tmp_path / "sessions")
    app.session_state.session_id = "saved"
    context = ImportedContext(
        ImportedSession(
            "codex",
            "original",
            "/old",
            [
                HarnessMessage("user", "Original task"),
                HarnessMessage("assistant", "**Original answer**"),
            ],
        )
    )
    context.save(app.session_manager.sessions_dir, "saved")
    app._restored_messages = [
        HumanMessage("Internal reference", id=MESSAGE_ID),
        HumanMessage("Continue here"),
    ]

    async def update(*_args, **_kwargs):
        pass

    async def state(*_args):
        return SimpleNamespace(values={"messages": app._restored_messages})

    app.agent.aupdate_state = update
    app.agent.aget_state = state
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await app._run_session_import("/context imported full")
        await pilot.pause()
        assert len(app._transcript().query(SessionTranscript)) == 1
        texts = [card.raw_text for card in app._transcript().query(ChatMessage)]
        assert "Original task" in texts
        assert "**Original answer**" in texts
        assert "Continue here" in texts
        assert "Internal reference" not in texts
        await app._run_clear()
        await pilot.pause()
        assert not app._transcript().query(SessionTranscript)


@pytest.mark.asyncio
async def test_large_history_bounds_widgets_and_releases_source():
    session = ImportedSession(
        "claude", "big", None, [HarnessMessage("assistant", "x" * 30000) for _ in range(100)]
    )
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(TranscriptScreen(session))
        await pilot.pause()
        history = app.screen.query_one(SessionTranscript)
        assert len(history.query(ChatMessage)) == 20
        assert all(len(card.raw_text) < 16200 for card in history.query(ChatMessage))
        assert "Display excerpt" in history.query(ChatMessage)[0].raw_text
        await pilot.press("escape")
        await pilot.pause()
        assert not history.session.messages
