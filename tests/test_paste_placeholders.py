"""Large pastes collapse to a placeholder that must round-trip AND be legible.

The prompt shows `[paste #N ...]` while composing and expands it back on submit.
The placeholder used to report only a line count, so a 100k-character
single-line paste rendered as "+0 lines" — indistinguishable from nothing
having been pasted at all.
"""

from __future__ import annotations

from novacode_cli.input_utils import (
    PasteTracker,
    format_paste_placeholder,
    resolve_paste_placeholders,
)


def _round_trip(text: str) -> tuple[str, str]:
    tracker = PasteTracker()
    paste_id = tracker.add_paste(text)
    placeholder = format_paste_placeholder(paste_id, text)
    return placeholder, resolve_paste_placeholders(placeholder, tracker)


def test_multiline_paste_reports_lines():
    placeholder, restored = _round_trip("line\n" * 200)
    assert "+200 lines" in placeholder
    assert restored == "line\n" * 200


def test_single_line_paste_reports_size_not_zero_lines():
    """The reported bug: a big single-line paste looked empty."""
    placeholder, restored = _round_trip("x" * 100_000)
    assert "0 lines" not in placeholder, placeholder
    assert "100.0k chars" in placeholder, placeholder
    assert restored == "x" * 100_000


def test_small_single_line_paste_reports_exact_chars():
    placeholder, _ = _round_trip("y" * 300)
    assert "300 chars" in placeholder, placeholder


def test_both_placeholder_shapes_resolve():
    """The resolver regex must match the char form as well as the line form."""
    tracker = PasteTracker()
    lines_id = tracker.add_paste("a\n" * 10)
    chars_id = tracker.add_paste("b" * 500)
    ten_lines = "a\n" * 10  # a backslash inside an f-string is a SyntaxError before 3.12
    text = (
        f"before {format_paste_placeholder(lines_id, ten_lines)} "
        f"middle {format_paste_placeholder(chars_id, 'b' * 500)} after"
    )
    restored = resolve_paste_placeholders(text, tracker)
    assert "[paste #" not in restored, "a placeholder was left unresolved"
    assert "a\na\n" in restored
    assert "b" * 500 in restored


def test_extended_paste_placeholder_grows():
    """A fragmented paste merges into one id; the size shown must follow it.

    Terminals split one paste into several Paste events, which are merged into a
    single block. The placeholder has to reflect the merged size, or a merge
    looks like the later content vanished.
    """
    tracker = PasteTracker()
    paste_id = tracker.add_paste("A" * 1000)
    first = format_paste_placeholder(paste_id, tracker.get_paste(paste_id))
    tracker.extend_paste(paste_id, "B" * 1000)
    second = format_paste_placeholder(paste_id, tracker.get_paste(paste_id))

    assert first != second, "merged paste showed the same size as before the merge"
    assert "1.0k chars" in first
    assert "2.0k chars" in second
    assert resolve_paste_placeholders(second, tracker) == "A" * 1000 + "B" * 1000
