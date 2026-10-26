"""Thinking and subsequent events must leave a reader's manual viewport alone."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from rich.text import Text
from textual.app import App
from textual.events import MouseScrollUp
from textual.widgets import Static

from novacode_cli import ui_events as ev
from novacode_cli.tui.widgets import ChatMessage
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from novacode_cli.tui.widgets import TranscriptScroll


def _wheel_up(transcript: TranscriptScroll) -> None:
    transcript.post_message(
        MouseScrollUp(
            widget=transcript,
            x=0,
            y=0,
            delta_x=0,
            delta_y=-1,
            button=0,
            shift=False,
            meta=False,
            ctrl=False,
        )
    )


@pytest.mark.timeout(30)
async def test_completed_reasoning_can_be_reopened_and_keeps_full_text(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    copied = Mock()
    monkeypatch.setattr(app, "copy_to_clipboard", copied)
    reasoning = "Beginning of reasoning. " + "detail " * 400 + " End of reasoning."
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._set_home_visible(visible=False)
        await app._render(ev.ReasoningDelta(reasoning))
        await pilot.pause()
        thought = app._reason_msg
        await app._render(ev.AssistantMessage("finished", "Nova", "cyan"))
        await pilot.pause()
        assert app._reason_msg is None
        assert thought.is_mounted
        assert thought._collapsed
        assert thought.raw_text == reasoning
        header = thought.query_one(".role", Static)
        body = thought.query_one(".body", Static)
        assert not body.display
        assert await pilot.click(header)
        await pilot.pause()
        assert not thought._collapsed
        assert body.display
        assert body.render().plain == reasoning
        copied.assert_not_called()

        # Header updates preserve the affordance, and subsequent turns keep it usable.
        thought.update_header(Text("Thinking complete"))
        assert "▾" in header.render().plain
        assert await pilot.click(header)
        await pilot.pause()
        assert thought._collapsed
        assert "▸" in header.render().plain
        await app._render(ev.ReasoningDelta("Next turn"))
        await app._render(ev.AssistantMessage("another answer", "Nova", "cyan"))
        await pilot.pause()
        assert len(app.query("ChatMessage.reason")) == 2
        assert await pilot.click(header)
        await pilot.pause()
        assert body.display
        assert thought.raw_text == reasoning


async def test_reasoning_keyboard_toggle_preserves_message_copy(monkeypatch: pytest.MonkeyPatch):
    app = App()
    copied = Mock()
    monkeypatch.setattr(app, "copy_to_clipboard", copied)
    thought = ChatMessage(Text("Thinking"), "reason", collapsible=True)
    thought.update_body(Text("Reasoning text"))
    answer = ChatMessage(Text("Nova"), "nova")
    answer.update_body(Text("Answer text"))
    async with app.run_test(size=(100, 30)) as pilot:
        await app.mount(thought, answer)
        await pilot.pause()
        header = thought.query_one(".role", Static)
        assert "▾" in header.render().plain
        header.focus()
        await pilot.press("enter")
        assert thought._collapsed
        await pilot.press("space")
        assert not thought._collapsed
        copied.assert_not_called()
        assert await pilot.click(thought.query_one(".body"))
        copied.assert_called_once_with("Reasoning text")
        copied.reset_mock()
        assert await pilot.click(answer.query_one(".role"))
        copied.assert_called_once_with("Answer text")
        assert not answer._collapsed


@pytest.mark.timeout(30)
async def test_reasoning_and_final_answer_do_not_pull_manual_scroll():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._set_home_visible(visible=False)
        transcript = app._transcript()
        await transcript.mount(Static("\n".join(f"history {index}" for index in range(150))))
        await app._render(ev.ReasoningDelta("checking\n" * 35))
        await pilot.pause()
        app.action_jump_latest()
        await pilot.pause()
        app.scroll_sensitivity_y = 1
        _wheel_up(transcript)
        await pilot.pause(0.2)
        assert not app._follow_tail
        before = transcript.scroll_y
        thought = app._reason_msg
        await app._render(ev.ReasoningDelta("more thinking"))
        app._flush_stream()
        await pilot.pause()
        assert transcript.scroll_y == before
        await app._render(ev.ToolCall("read_file", "sample.py", "", call_id="read"))
        await app._render(ev.ToolResult("read complete", call_id="read"))
        await pilot.pause()
        assert transcript.scroll_y == before
        await app._render(ev.AssistantMessage("finished", "Nova", "cyan"))
        await pilot.pause()
        assert transcript.scroll_y == before
        assert not app._follow_tail
        assert not thought._collapsed
        # Wheel/keyboard scrolling still works after the final answer arrives.
        _wheel_up(transcript)
        await pilot.pause(0.2)
        assert transcript.scroll_y < before
        app.action_jump_latest()
        await pilot.pause()
        assert app._follow_tail
        assert transcript.is_vertical_scroll_end
