"""Thinking and subsequent events must leave a reader's manual viewport alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.events import MouseScrollUp
from textual.widgets import Static

from novacode_cli import ui_events as ev
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
