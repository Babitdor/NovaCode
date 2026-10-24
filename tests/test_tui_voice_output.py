"""Automatic speech receives only enabled, main assistant prose."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from novacode_cli import ui_events as ev
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app

if TYPE_CHECKING:
    import pytest


async def test_tool_reasoning_and_subagent_events_do_not_enter_speech():
    app = _app()
    async with app.run_test() as pilot:
        app._voice_pipeline = SimpleNamespace()
        app._voice_speak_responses = True
        app._accumulated_reply = ""
        app._speak_reply = Mock()
        app._sync_async_task_watcher = AsyncMock()
        await app._render(ev.AssistantMessage("Nova's answer.", "Nova", "cyan"))
        await app._render(ev.ToolCall("test_tool", "PRIVATE TOOL CALL", "*"))
        await app._render(ev.ToolResult("PRIVATE TOOL RESULT"))
        await app._render(ev.ReasoningDelta("PRIVATE REASONING"))
        await app._render(
            ev.AssistantMessage("PRIVATE SUBAGENT", "Worker", "blue", is_subagent=True)
        )
        await app._render(ev.Done(had_response=True))
        await pilot.pause()
        assert app._speak_reply.call_count == 1
        assert app._speak_reply.call_args.args[0].strip() == "Nova's answer."


async def test_voice_disabled_during_summary_does_not_speak(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    started, release = asyncio.Event(), asyncio.Event()

    async def summary(_text: str) -> str:
        started.set()
        await release.wait()
        return "Short summary."

    monkeypatch.setattr("novacode_cli.audio.summarize.summarize_for_speech", summary)
    async with app.run_test():
        pipeline = SimpleNamespace(speak=AsyncMock(), tts_needs_download=False)
        app._voice_pipeline = pipeline
        app._voice_speak_responses = True
        worker = app._speak_reply("A response.")
        await started.wait()
        app._voice_speak_responses = False
        release.set()
        await worker.wait()
        pipeline.speak.assert_not_called()
