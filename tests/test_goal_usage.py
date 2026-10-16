"""Offline goal replays keep provider usage and context accounting distinct."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli import ui_events as ev
from novacode_cli.commands.side_commands import handle_goal_command
from novacode_cli.context import ContextBreakdown
from novacode_cli.states.Session import SessionState
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.home import SystemSampler
from novacode_cli.ui.ui_elements import TokenTracker
from tests.test_agent_stream import _Chunk
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


class CompactingGoalAgent:
    """Two main requests separated by compaction and a goal continuation."""

    def __init__(self) -> None:
        self.turn = 0
        self.values: dict = {"messages": []}

    async def aget_state(self, _config: object) -> SimpleNamespace:
        return SimpleNamespace(values=self.values)

    async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
        self.turn += 1
        if self.turn == 1:
            yield ((), "custom", {"type": "nova_compaction_start"})
            yield ((), "custom", {"type": "nova_compaction_end"})
        answer = "progress" if self.turn == 1 else "GOAL ACHIEVED\nEvidence: tests passed"
        usage = {"input_tokens": 1100 + self.turn * 100, "output_tokens": 100}
        yield (
            (),
            "messages",
            (_Chunk(f"request-{self.turn}", [{"type": "text", "text": answer}], usage=usage), {}),
        )
        self.values = {
            "messages": [HumanMessage("task"), AIMessage(answer)],
            "_summarization_event": {
                "cutoff_index": 1,
                "summary_message": HumanMessage("summary"),
            },
            "_context_snapshot": {
                "estimated_input_tokens": 2700 + self.turn * 100,
                "provider_input_tokens": usage["input_tokens"],
            },
        }


async def test_goal_compaction_keeps_newer_provider_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.agent_stream._maybe_verifier", lambda: None)
    monkeypatch.setattr(SystemSampler, "sample", lambda _sampler: None)
    state = SessionState()
    handle_goal_command(state, "Complete the task and run its tests")
    tracker = TokenTracker()
    tracker.set_model("gpt-4o")
    app = NovaApp(
        agent=CompactingGoalAgent(),
        assistant_id="nova-agent",
        session_state=state,
        backend=None,
        token_tracker=tracker,
        image_tracker=None,
        model_name="gpt-4o",
    )
    render = app._render
    compacted_contexts: list[int] = []
    usage_totals: list[int] = []

    async def observe(event: object) -> None:
        await render(event)
        if isinstance(event, ev.CompactionNotice):
            compacted_contexts.append(tracker.get_breakdown().total_tokens)
        if isinstance(event, ev.UsageUpdate):
            usage_totals.append(tracker.session_total_tokens)

    monkeypatch.setattr(app, "_render", observe)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await app._stream_prompt("Start the goal")
        assert compacted_contexts == [1200]
        assert usage_totals == [1300, 2700]
        assert tracker.get_breakdown().total_tokens == 1300
        assert state.goal_status == "complete"


async def test_live_usage_updates_both_indicators_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SystemSampler, "sample", lambda _sampler: None)
    tracker = TokenTracker()
    app = NovaApp(
        agent=CompactingGoalAgent(),
        assistant_id="nova-agent",
        session_state=SessionState(),
        backend=None,
        token_tracker=tracker,
        image_tracker=None,
        model_name="gpt-4o",
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        tracker.set_breakdown(ContextBreakdown(total_tokens=1000, context_window_size=10000))
        tracker.current_context = 1000
        app._refresh_usage_metrics()
        await app._render(ev.ContextUsage(input_tokens=2000))
        assert "20%" in str(app.query_one("#prompt-hint-bar").render())
        assert "ctx 20%" in str(app.query_one("#info-quota").render())
        await app._render(ev.UsageUpdate(input_tokens=2000, output_tokens=300))
        assert "2k used" in str(app.query_one("#info-quota").render())


async def test_remote_goal_usage_sums_turns_and_uses_latest_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from rich.console import Console

    from novacode_cli.ui import execution
    from novacode_cli.vixie import server

    for name in ("set_idle", "set_thinking", "set_working"):
        monkeypatch.setattr(server, name, AsyncMock())
    monkeypatch.setattr(execution, "console", Console(file=io.StringIO()))

    async def source(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield ev.UsageUpdate(
            input_tokens=2000,
            output_tokens=30,
            session_tokens=2030,
            session_input_tokens=2000,
            cache_read_tokens=1000,
        )
        yield ev.CompactionNotice()
        yield ev.UsageUpdate(
            input_tokens=500,
            output_tokens=40,
            session_tokens=540,
            session_input_tokens=500,
            cache_read_tokens=100,
        )
        yield ev.AssistantMessage("done", "Nova", "cyan")
        yield ev.Done(had_response=True)

    monkeypatch.setattr(execution, "run_with_goal", source)
    tracker = TokenTracker()
    await execution.execute_task(
        "continue",
        CompactingGoalAgent(),
        "nova-agent",
        SessionState(),
        token_tracker=tracker,
    )
    assert tracker.current_context == 500
    assert tracker.session_total_tokens == 2570
    assert tracker.session_input_tokens == 2500
    assert tracker.session_cache_read_tokens == 1100
