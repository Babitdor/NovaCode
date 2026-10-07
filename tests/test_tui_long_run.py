"""Long turns must not grow display memory or copy full streams for each frame."""

from types import SimpleNamespace

import pytest
from rich.text import Text
from textual.app import App
from textual.containers import VerticalScroll

from novacode_cli import ui_events as ev
from novacode_cli.tui import app as app_module
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.widgets import ChatMessage, OutputLog


async def test_transcript_byte_budget_preserves_active_content(monkeypatch):
    monkeypatch.setattr(app_module, "_MAX_TRANSCRIPT_CHARS", 10_000)
    monkeypatch.setattr(app_module, "_TRANSCRIPT_CHARS_LOW_WATER", 6_000)

    class TranscriptApp(App):
        def compose(self):
            yield VerticalScroll(id="transcript")

    app = TranscriptApp()
    async with app.run_test() as pilot:
        tr = app.query_one("#transcript")
        active = ChatMessage(Text("Live"), "nova")
        active.update_body(Text("p" * 2000))
        await tr.mount(active)
        history = []
        for index in range(8):
            output = OutputLog()
            output.write(f"{index}:" + "x" * 1800)
            history.append(output)
            await tr.mount(output)
        await pilot.pause()
        state = SimpleNamespace(
            _transcript=lambda: tr,
            _stream_msg=active,
            _reason_msg=None,
            _tool_group=None,
            _tool_components={},
            _subagent_widgets={},
            _last_tool=None,
        )
        NovaApp._prune_transcript(state)
        await pilot.pause()
        assert active in tr.children
        assert history[-1] in tr.children
        assert len(tr.children) < 8  # count limit alone would retain all of them
        retained = len(active.raw_text) + sum(log._output_chars for log in history)
        assert retained <= app_module._TRANSCRIPT_CHARS_LOW_WATER


def test_live_preview_reads_only_the_tail_without_flattening_stream():
    parts = ["old" * 100_000, "latest", " text"]
    assert NovaApp._rope_tail(parts, 11) == "latest text"
    assert len(parts) == 3
    state = SimpleNamespace(
        _stream_msg=SimpleNamespace(update_body=lambda body: painted.append(body.plain)),
        _reason_msg=None,
        _live_buf_parts=parts,
        _scroll_end=lambda **kwargs: None,
        _rope_tail=NovaApp._rope_tail,
    )
    painted = []
    NovaApp._paint_stream(state)
    assert painted == ["".join(parts)[-app_module._LIVE_PREVIEW_CHARS :]]
    assert parts[1:] == ["latest", " text"]


@pytest.mark.parametrize("speech", [False, True])
async def test_long_turn_only_keeps_bounded_speech_when_enabled(speech):
    class Body:
        def update_header(self, header):
            pass

        def update_body(self, body):
            pass

    async def noop():
        pass

    state = SimpleNamespace(
        _stream_msg=Body(),
        _live_buf_parts=[],
        _remove_reasoning=noop,
        _scroll_end=lambda: None,
        _agent_label=lambda *args: Text("Nova"),
        _voice_pipeline=object() if speech else None,
        _voice_speak_responses=speech,
        _accumulated_reply="",
        _schedule_prune=lambda: None,
    )
    for _ in range(20):
        state._stream_msg = Body()
        await NovaApp._render(
            state, ev.AssistantMessage(text="x" * 20_000, agent_name="Nova", agent_color=None)
        )
    assert len(state._accumulated_reply) == (16_000 if speech else 0)


async def test_removed_chat_message_releases_markdown_and_copy_buffer():
    class TranscriptApp(App):
        def compose(self):
            yield VerticalScroll(id="transcript")

    app = TranscriptApp()
    async with app.run_test() as pilot:
        message = ChatMessage(Text("Nova"), "nova")
        message.update_body(app_module.Markdown("large answer " * 200))
        await app.query_one("#transcript").mount(message)
        await pilot.pause()
        body = message.query_one(".body")
        await message.remove()
        await pilot.pause()
        assert message.raw_text == ""
        assert str(body.content) == ""
        assert not body._styles_cache._cache
        message.update_body(Text("late stream callback"))
        assert message.raw_text == ""
