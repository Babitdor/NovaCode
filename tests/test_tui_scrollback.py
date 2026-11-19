"""Regression coverage for conversation scrollback paging."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from rich.text import Text
from textual.app import App
from textual.events import MouseScrollUp
from textual.widgets import Button

from novacode_cli.context.archives import RecoveryStore
from novacode_cli.session.adapters import HarnessMessage
from novacode_cli.tui import app as app_module
from novacode_cli.tui import scrollback
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.code_mode_panel import CodeModePanel
from novacode_cli.tui.widgets import ChatMessage, TranscriptScroll


class TranscriptApp(App):
    def compose(self):
        yield TranscriptScroll(id="transcript")


def _card(text: str) -> ChatMessage:
    card = ChatMessage(Text("Nova"), "nova")
    card.update_body(Text(text))
    return card


async def _settle(page, pilot) -> None:
    await page.workers.wait_for_complete()
    await pilot.pause(0.05)


async def test_older_pages_keep_the_visible_anchor_and_bound_widgets(monkeypatch):
    monkeypatch.setattr(scrollback, "PAGE_ROWS", 3)
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        tr = app.query_one(TranscriptScroll)
        live = [_card(f"live {i}") for i in range(8)]
        await tr.mount(*live)
        tr.set_older_history(lambda: [HarnessMessage("assistant", f"old {i}") for i in range(15)])
        await pilot.pause()
        page = tr._scrollback
        live_top = live[0].region.y

        tr.post_message(MouseScrollUp(tr, 0, 0, 0, -1, 0, False, False, False))
        await pilot.pause()
        await _settle(page, pilot)
        assert [card.raw_text for card in page.query(ChatMessage)] == ["old 12", "old 13", "old 14"]
        assert live[0].region.y == live_top

        assert not page.query(Button)
        for _ in range(3):
            anchor = page._pages[0][2]
            if tr.scroll_y <= 1:
                tr.action_page_up()
            else:
                tr.scroll_home(animate=False, immediate=True)
            await _settle(page, pilot)
            assert anchor.region.y == tr.scrollable_content_region.y
            assert len(page._pages) <= 3

        assert [card.raw_text for card in page.query(ChatMessage)] == [
            f"old {i}" for i in range(3, 12)
        ]
        assert page._gap.display
        seam = page._gap.virtual_region.y
        anchor = page._pages[-1][2]
        target = seam - tr.scrollable_content_region.height + 1
        expected_top = anchor.virtual_region.y - target
        tr.scroll_to(y=target, animate=False, immediate=True)
        await _settle(page, pilot)
        assert anchor.region.y == expected_top
        assert [card.raw_text for card in page.query(ChatMessage)][-1] == "old 14"
        assert not page._gap.display
        assert all(card.parent is tr for card in live)


async def test_pruned_live_messages_are_reachable_and_reset_with_the_pane(monkeypatch):
    monkeypatch.setattr(app_module, "_MAX_TRANSCRIPT_WIDGETS", 7)
    monkeypatch.setattr(app_module, "_TRANSCRIPT_LOW_WATER", 4)
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        tr = app.query_one(TranscriptScroll)
        cards = [_card(f"message {i}") for i in range(10)]
        await tr.mount(*cards)
        state = SimpleNamespace(
            _transcript=lambda: tr,
            _stream_msg=None,
            _reason_msg=None,
            _tool_group=None,
            _tool_components={},
            _subagent_widgets={},
            _last_tool=None,
        )
        NovaApp._prune_transcript(state)
        await pilot.pause()
        assert cards[0] not in tr.children and cards[-1] in tr.children
        page = tr._scrollback
        tr.action_page_up()
        await _settle(page, pilot)
        assert [card.raw_text for card in page.query(ChatMessage)] == [
            f"message {i}" for i in range(6)
        ]
        tr.reset_scrollback()
        await tr.remove_children()
        assert page._live.file.closed
        assert tr._scrollback is None


def test_saved_scrollback_reaches_before_compaction_and_deduplicates(tmp_path, monkeypatch):
    store = RecoveryStore(tmp_path)
    monkeypatch.setattr("novacode_cli.context.archives.default_recovery_store", lambda: store)
    older = HumanMessage(content="old prompt", id="old")
    visible = AIMessage(content="new reply", id="new")
    manifest = store.archive("current", [older, visible])
    foreign = store.archive("other", [HumanMessage(content="other transcript")])
    marker = HumanMessage(
        content="compact summary",
        additional_kwargs={
            "lc_source": "summarization",
            "history_files": [str(manifest), str(foreign)],
        },
    )
    rows = scrollback.saved_rows(None, None, [marker, older], [visible], thread_id="current")
    assert [row.content for row in rows] == ["old prompt"]


@pytest.mark.parametrize("name,args", [
    ("grep", {"pattern": "test"}),
    ("eval", {"code": "console.log('test')"}),
])
async def test_saved_and_pruned_tools_use_the_live_card_with_exact_details(name, args):
    from novacode_cli.session.adapters.harness_native import normalize_messages

    result = '[red]failed[/red]\nexact output'
    call = {"name": name, "id": "call", "args": args}
    records = normalize_messages([
        AIMessage("", tool_calls=[call]),
        ToolMessage(result, name=name, tool_call_id="call", status="error"),
    ], pair_tools=True)
    assert len(records) == 1
    app = TranscriptApp()

    async with app.run_test(size=(80, 24)) as pilot:
        live = CodeModePanel.for_call(name, args)
        live.finish(result, is_error=True, preview="failed")
        tr = app.query_one(TranscriptScroll)
        tr.set_older_history(lambda: records)
        tr.archive_removed([live])
        await pilot.pause()
        tr.action_page_up()
        await _settle(tr._scrollback, pilot)

        restored = list(tr.query(CodeModePanel))
        assert len(restored) == 2
        for card in restored:
            assert card.heading.content == live.heading.content
            assert card.preview.content == live.preview.content
            assert card.has_class("tool", "failed")
            assert card.collapsed and card.output is None
            card.collapsed = False
            await pilot.pause()
            assert card.source.content.plain == live._source_text
            assert card.output.content.plain == result


async def test_pruned_edit_keeps_its_diff_preview():
    from novacode_cli.tui.diff_preview import DiffPreview

    app = TranscriptApp()
    async with app.run_test(size=(110, 30)) as pilot:
        tr = app.query_one(TranscriptScroll)
        live = CodeModePanel.for_call("edit_file", {"path": "test.py"})
        live.finish("Updated test.py", is_error=False, preview="+1 / -1")
        diff = "@@ -1 +1 @@\n-old\n+new"
        classic = Text("-old\n+new")
        classic.stylize("green", 5)
        live.output.set_diff(diff, "test.py", classic, "classic")
        live.collapsed = False
        tr.archive_removed([live])
        await pilot.pause()
        tr.action_page_up()
        await _settle(tr._scrollback, pilot)

        restored = tr.query_one(CodeModePanel)
        assert not restored.collapsed
        preview = restored.query_one(DiffPreview)
        assert preview._classic == classic
        assert preview._classic.spans == classic.spans
        assert preview._rows == live.output._rows
        assert preview._path == "test.py"
