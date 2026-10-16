"""Exercise real history replay alongside input, tools, and modal screens."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from rich.text import Text

from novacode_cli import ui_events as ev
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.screens import ConfirmModal
from novacode_cli.tui.session_pane import fresh_state
from novacode_cli.tui.widgets import ChatMessage, OutputLog, PromptInput
from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("style", ["dashboard", "classic", "centered", "banner", "minimal"])
async def test_replay_preserves_output_input_and_modals(
    style: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "novacode_cli.tui.harness.profile_path", lambda _workspace: tmp_path / "layout.json"
    )
    monkeypatch.setattr(NovaApp, "_refresh_branch_worker", lambda _app: None)
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        app._set_home_style(style)
        await pilot.pause()
        child = await _add_pane(app)
        child.state = fresh_state(session_state=app.session_state, model_name="test")
        expected = [
            f"History {index}: **result**\n\nDetails remain copyable." for index in range(80)
        ]
        for text in expected:
            await app._deliver(child, ev.AssistantMessage(text, "nova", "cyan"))
        await app._switch_to(child)
        assert child.buffer  # Switching returns before the transcript is rebuilt.
        await pilot.press("h", "i")

        worker = app._pane_replay_workers.get(child.sid)
        if worker is not None:
            await asyncio.wait_for(worker.wait(), timeout=30)
        await app._deliver(child, ev.TextDelta("partial"))
        await app._deliver(child, ev.AssistantMessage("complete **answer**", "nova", "cyan"))
        await app._deliver(child, ev.ToolCall("shell", "offline command", "$", call_id="check"))
        app._on_tool_output("check", "first line\nfinal line\n")
        app._flush_tool_output()
        await app._deliver(
            child, ev.ToolResult("done", full_output="first line\nfinal line\n", call_id="check")
        )
        await app._deliver(child, ev.Done())
        assert not app._turn_active
        assert app._stream_msg is None
        await app._deliver(child, ev.TextDelta("next turn"))
        await app._deliver(child, ev.AssistantMessage("next answer", "nova", "cyan"))
        await app._deliver(child, ev.Done())
        await pilot.pause()
        messages = list(child.scroll.query(ChatMessage))
        assert [message.raw_text for message in messages] == [
            *expected,
            "complete **answer**",
            "next answer",
        ]
        assert any(
            "final line" in line.plain
            for output in child.scroll.query(OutputLog)
            for line in output._output_lines
        )
        assert child.scroll.max_scroll_y > 0
        assert app.query_one("#prompt", PromptInput).value == "hi"
        modal = ConfirmModal("Replay check", Text("The long transcript remains available."))
        app.push_screen(modal)
        await pilot.pause()
        assert app.screen is modal
        modal.dismiss(result=False)
        await pilot.pause()
        assert app.query_one("#prompt", PromptInput).value == "hi"
        assert not child.buffer
        assert not app._turn_active
