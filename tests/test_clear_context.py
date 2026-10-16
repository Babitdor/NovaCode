"""Clearing a conversation keeps the complete empty-chat context estimate."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.states.Session import SessionState
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.home import SystemSampler
from novacode_cli.ui.ui_elements import TokenTracker
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.parametrize("missing_checkpoint", [False, True])
async def test_clear_counts_tools_before_context_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing_checkpoint: bool,
) -> None:
    class Agent:
        async def aget_state(self, _config: object) -> SimpleNamespace | None:
            return None if missing_checkpoint else SimpleNamespace(values={})

        async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
            return
            yield

    monkeypatch.setattr(SystemSampler, "sample", lambda _sampler: None)
    tracker = TokenTracker()
    tracker.set_model("gpt-4o")
    tracker.set_baseline(700)
    state = SessionState()
    state._tools = [
        {
            "name": "read_file",
            "description": "Read workspace files. " * 500,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    app = NovaApp(
        agent=Agent(),
        assistant_id="nova-agent",
        session_state=state,
        backend=None,
        token_tracker=tracker,
        image_tracker=None,
        model_name="gpt-4o",
    )
    monkeypatch.setattr(app, "_consolidate_learning", AsyncMock())
    async with app.run_test(size=(170, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        expected = tracker.get_breakdown()
        assert expected.tool_definitions_tokens > 0
        old_thread = state.thread_id
        tracker.add(8000, 100)
        await app._run_clear()
        cleared = tracker.get_breakdown()
        assert state.thread_id != old_thread
        assert cleared.total_tokens == expected.total_tokens
        assert cleared.tool_definitions_tokens == expected.tool_definitions_tokens
        assert cleared.conversation_tokens == 0
        assert tracker.session_total_tokens == 0
        assert not tracker.has_api_data
        status = str(app.query_one("#prompt-hint-bar").render())
        quota = str(app.query_one("#info-quota").render())
        home = str(app.query_one("#home-context").render())
        await app._run_context()
        await pilot.pause()
        assert tracker.get_breakdown().total_tokens == cleared.total_tokens
        assert str(app.query_one("#prompt-hint-bar").render()) == status
        assert str(app.query_one("#info-quota").render()) == quota
        assert str(app.query_one("#home-context").render()) == home
        app.pop_screen()
        await pilot.pause()
