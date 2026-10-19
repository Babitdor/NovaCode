"""The composer takes a paste of any size: shown briefly, sent in full."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from textual import events

from novacode_cli.input_utils import (
    PasteTracker,
    format_paste_placeholder,
    resolve_paste_placeholders,
)


def _blob(lines: int) -> str:
    return "\n".join(f"line {i}: the quick brown fox" for i in range(lines))


def test_a_paste_too_big_for_the_window_is_saved_whole_and_referenced(tmp_path):
    tracker = PasteTracker()
    text = _blob(200_000)
    paste_id = tracker.add_paste(text)
    sent = resolve_paste_placeholders(
        f"summarise this:\n{format_paste_placeholder(paste_id, text)}",
        tracker, spill_over=100_000, spill_dir=tmp_path,
    )
    assert sent.startswith("summarise this:"), "typed instructions stay inline"
    path = Path(re.search(r"saved in full at (.+?\.txt)", sent).group(1))
    assert path.read_text(encoding="utf-8") == text, "nothing is cut"
    assert "line 0:" in sent and "line 39:" in sent and "line 40:" not in sent
    assert len(sent) < 5_000
    assert (tmp_path / ".gitignore").read_text() == "*\n"


def test_a_paste_that_fits_is_sent_inline(tmp_path):
    tracker = PasteTracker()
    text = _blob(500)
    paste_id = tracker.add_paste(text)
    sent = resolve_paste_placeholders(
        format_paste_placeholder(paste_id, text), tracker, spill_over=100_000, spill_dir=tmp_path
    )
    assert sent == text and not list(tmp_path.iterdir())


def test_a_huge_message_is_drawn_as_its_opening_lines():
    from novacode_cli.tui.app import _user_message_body

    body = _user_message_body(_blob(50_000))
    assert body.plain.count("\n") <= 41 and "more lines" in body.plain and "sent in full" in body.plain
    assert _user_message_body("short").plain == "short"


@pytest.mark.asyncio
async def test_a_multi_megabyte_paste_goes_through_the_composer(monkeypatch, tmp_path):
    from novacode_cli.config.config import settings
    from tests.test_tui_compaction_row import _app

    monkeypatch.setattr(settings, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setenv("NOVA_INPUT_HISTORY", str(tmp_path / "history.jsonl"))
    app = _app()
    sent: list[str] = []
    text = _blob(120_000)  # ~3.5 MB
    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "_dispatch", sent.append)
        box = app.query_one("#prompt")
        box.post_message(events.Paste(text))
        await pilot.pause()
        assert box.text.startswith("[paste #1"), "the box shows a placeholder"
        await pilot.pause(1.7)
        await pilot.press("enter")
        await pilot.pause()
        assert box._history == sent and len(sent[0]) < 5_000, "Up recalls the short reference"
    path = Path(re.search(r"saved in full at (.+?\.txt)", sent[0]).group(1))
    assert path.read_text(encoding="utf-8") == text
    assert path.parent == tmp_path / ".nova" / "pastes"


@pytest.mark.asyncio
async def test_a_huge_inline_input_is_not_kept_for_recall(monkeypatch, tmp_path):
    from novacode_cli.tui.app import _HISTORY_MAX_CHARS
    from tests.test_tui_compaction_row import _app

    monkeypatch.setenv("NOVA_INPUT_HISTORY", str(tmp_path / "history.jsonl"))
    app = _app()
    async with app.run_test(size=(120, 40)):
        box = app.query_one("#prompt")
        app._remember_input(box, "x" * (_HISTORY_MAX_CHARS + 1))
        app._remember_input(box, "hello")
    assert box._history == ["hello"]
