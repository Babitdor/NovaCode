"""Experimental split previews preserve source alignment and appearance preferences."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.style import Style
from textual.widgets import Select

from novacode_cli import ui_events as ev
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.file_ops import FileOperationRecord
from novacode_cli.tui.diff_preview import DiffLine, DiffPreview, diff_preview_style, diff_rows
from novacode_cli.tui.screens import ThemeScreen
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, _resize_terminal, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

DIFF = (
    "--- example.py (before)\n"
    "+++ example.py (after)\n"
    "@@ -9,3 +9,4 @@\n"
    " def greet(name):\n"
    "-    return 'old'\n"
    "+    message = 'hello'\n"
    "+    return message + name\n"
    " # done"
)


def test_change_blocks_align_and_keep_independent_line_numbers():
    rows = diff_rows(DIFF)
    assert rows[3].before == DiffLine(9, "def greet(name):")
    assert rows[4].before == DiffLine(10, "    return 'old'", "-")
    assert rows[4].after == DiffLine(10, "    message = 'hello'", "+")
    assert rows[5].before is None
    assert rows[5].after == DiffLine(11, "    return message + name", "+")
    assert rows[6].before == DiffLine(11, "# done")
    assert rows[6].after == DiffLine(12, "# done")


def test_insert_delete_multiple_hunks_and_header_like_source():
    rows = diff_rows("@@ -0,0 +1,1 @@\n+first\n@@ -94,2 +95,0 @@\n--- code\n-last")
    assert rows[1].before is None
    assert rows[1].after == DiffLine(1, "first", "+")
    assert rows[3].before == DiffLine(94, "-- code", "-")
    assert rows[3].after is None
    assert rows[4].before == DiffLine(95, "last", "-")


def test_annotation_and_truncation_do_not_invent_source_lines():
    rows = diff_rows("@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file", max_lines=3)
    assert rows[1].before == DiffLine(1, "old", "-")
    assert rows[1].after == DiffLine(1, "new", "+")
    assert rows[-1].note == "… 1 more lines"
    assert diff_rows("") == []
    assert diff_rows("+new")[0].after == DiffLine(1, "new", "+")


@pytest.mark.parametrize("value", [None, "classic", "bad", True, 1])
def test_missing_or_invalid_preference_stays_classic(value: object):
    assert diff_preview_style(value) == "classic"


@pytest.mark.timeout(45)
async def test_themes_switch_existing_diffs_resize_and_persist(tmp_path: Path):  # noqa: PLR0915 -- one preview lifecycle.
    app = _app()
    async with app.run_test(size=(150, 42)) as pilot:
        app._set_home_visible(visible=False)
        await app._render(ev.ToolCall("edit_file", "example.py", "", call_id="edit"))
        record = FileOperationRecord("edit_file", "example.py", None, "edit", diff=DIFF)
        await app._render(ev.FileOp(record, call_id="edit"))
        await pilot.pause()
        preview = app.query_one(DiffPreview)
        classic = preview.render()
        assert classic.plain == app._render_diff_text(DIFF, path="example.py").plain
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        selector = screen.query_one("#diff-preview-select", Select)
        assert selector.value == "classic"
        selector.value = "side-by-side"
        await pilot.pause()
        assert NovaConfig().get("diff_preview_style") == "side-by-side"
        assert preview.preview_style == "side-by-side"
        screen.dismiss()
        await pilot.pause()
        split = preview.render()
        assert "Before" in split.plain
        assert "After" in split.plain
        assert "return 'old'" in split.plain
        assert "return message + name" in split.plain
        assert any(Style.parse(str(span.style)).bgcolor is not None for span in split.spans)
        (tmp_path / "diff-split-dark.svg").write_text(app.export_screenshot(), encoding="utf-8")
        for theme in ("textual-light", "tokyo-night"):
            app.theme = theme
            await pilot.pause()
            assert "Before" in preview.render().plain
            (tmp_path / f"diff-{theme}.svg").write_text(app.export_screenshot(), encoding="utf-8")
        await _resize_terminal(pilot, 48, 24)
        assert preview.render().plain == classic.plain
        await _resize_terminal(pilot, 150, 42)
        assert "Before" in preview.render().plain
        app._set_diff_preview_style("classic")
        assert preview.render().plain == classic.plain
    restarted = _app()
    async with restarted.run_test() as pilot:
        await pilot.pause()
        assert restarted._diff_preview_style == "side-by-side"
        screen = ThemeScreen()
        restarted.push_screen(screen)
        await pilot.pause()
        selector = screen.query_one("#diff-preview-select", Select)
        assert selector.value == "side-by-side"
        selector.value = "classic"
        await pilot.pause()
        assert NovaConfig().get("diff_preview_style") == "classic"
        assert restarted._diff_preview_style == "classic"


@pytest.mark.timeout(30)
async def test_split_preserves_wrapped_unicode_tabs_and_markup_literal():
    app = _app()
    code = "\tprint('[bold]生涯最愛[/bold] " + "longword" * 16 + "')"
    diff = "@@ -1 +1 @@\n-old\n+" + code
    async with app.run_test(size=(120, 42)) as pilot:
        app._set_home_visible(visible=False)
        preview = DiffPreview()
        await app._transcript().mount(preview)
        preview.set_diff(diff, "sample.py", app._render_diff_text(diff), "side-by-side")
        await pilot.pause()
        rendered = preview.render()
        assert "[bold]" in rendered.plain
        assert "生涯最愛" in rendered.plain
        assert len(rendered.plain.splitlines()) > 4
        assert all(line.cell_len <= preview.content_size.width for line in rendered.split("\n"))
        preview.set_diff("+created", "new.txt", app._render_diff_text("+created"), "side-by-side")
        assert "created" in preview.render().plain
