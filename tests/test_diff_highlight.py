"""Tests for per-line syntax highlighting of diff previews.

Covers the shared helper (``novacode_cli.ui.diff_highlight``) and both render
paths: the Rich console path (``format_diff_rich``) and the Textual TUI path
(``NovaApp._render_diff_text``).
"""

from __future__ import annotations

from novacode_cli.ui.diff_highlight import (
    has_highlighting,
    highlight_line,
    lexer_for_path,
)
from novacode_cli.ui.ui_elements import format_diff_rich

SAMPLE_DIFF = [
    "@@ -1,3 +1,3 @@",
    " def f(x):",
    "-    return x",
    "+    return x + 1",
    " # comment",
]


class TestLexerForPath:
    """lexer_for_path: extension -> Pygments lexer, or None."""

    def test_python_extension(self):
        lexer = lexer_for_path("foo.py")
        assert lexer is not None
        assert type(lexer).__name__ == "PythonLexer"

    def test_title_with_prefix(self):
        # render_diff_block passes titles like "Diff foo.py".
        lexer = lexer_for_path("Diff foo.py")
        assert lexer is not None
        assert type(lexer).__name__ == "PythonLexer"

    def test_unknown_extension_returns_none(self):
        assert lexer_for_path("foo.zzznotalang") is None

    def test_no_extension_returns_none(self):
        assert lexer_for_path("Makefile") is None

    def test_none_returns_none(self):
        assert lexer_for_path(None) is None


class TestHighlightLine:
    """highlight_line: tokenize a single line into (text, style) spans."""

    def test_python_line_has_multiple_styled_spans(self):
        lexer = lexer_for_path("foo.py")
        spans = highlight_line("def f(x): return x", lexer)
        assert has_highlighting(spans)
        # Keyword and function name should carry distinct styles.
        styles = {style for _, style in spans if style}
        assert len(styles) >= 2

    def test_no_lexer_returns_single_unstyled_span(self):
        spans = highlight_line("def f(x): return x", None)
        assert spans == [("def f(x): return x", "")]
        assert not has_highlighting(spans)

    def test_empty_line(self):
        assert highlight_line("", lexer_for_path("foo.py")) == [("", "")]

    def test_spans_reconstruct_original_text(self):
        lexer = lexer_for_path("foo.py")
        code = "def f(x): return x + 1"
        spans = highlight_line(code, lexer)
        assert "".join(text for text, _ in spans) == code


class TestFormatDiffRich:
    """format_diff_rich: Rich markup with syntax colours + diff markers."""

    def test_python_diff_contains_syntax_colour(self):
        out = format_diff_rich(SAMPLE_DIFF, path="foo.py")
        # monokai keyword colour for "def"/"return".
        assert "#66d9ef" in out

    def test_python_diff_keeps_marker_backgrounds(self):
        out = format_diff_rich(SAMPLE_DIFF, path="foo.py")
        assert "white on dark_green" in out
        assert "white on dark_red" in out

    def test_unknown_extension_has_no_syntax_colour(self):
        out = format_diff_rich(SAMPLE_DIFF, path="foo.zzznotalang")
        assert "#66d9ef" not in out
        # Diff markers still present.
        assert "white on dark_green" in out

    def test_no_path_has_no_syntax_colour(self):
        out = format_diff_rich(SAMPLE_DIFF)
        assert "#66d9ef" not in out

    def test_empty_diff(self):
        assert format_diff_rich([]) == "[dim]No changes detected[/dim]"

    def test_long_line_wraps_and_keeps_highlighting(self):
        long_code = "x = " + " + ".join(["1"] * 200)
        diff = ["@@ -1 +1 @@", f"+{long_code}"]
        out = format_diff_rich(diff, path="foo.py")
        # Wrapped into multiple rendered lines, still highlighted.
        assert out.count("\n") >= 1
        assert "#ae81ff" in out  # monokai number colour


class TestTuiDiffText:
    """NovaApp._render_diff_text: Text spans with syntax + marker colours."""

    def test_python_diff_has_syntax_and_marker_styles(self):
        from novacode_cli.tui.app import NovaApp

        diff = "\n".join(SAMPLE_DIFF)
        text = NovaApp._render_diff_text(diff, path="foo.py")
        styles = {str(span.style) for span in text.spans}
        # Syntax colour present, overlaid on the marker background.
        assert any("#66d9ef" in s for s in styles)
        # Marker colour is a filled background block, not bare foreground text.
        assert any("on dark_green" in s for s in styles)
        assert any("on dark_red" in s for s in styles)

    def test_marker_uses_background_not_bare_foreground(self):
        """The +/- signal must be a background block, not just coloured text."""
        from novacode_cli.tui.app import NovaApp

        diff = "\n".join(SAMPLE_DIFF)
        text = NovaApp._render_diff_text(diff, path="foo.py")
        styles = {str(span.style) for span in text.spans}
        # A bare "green"/"red" foreground style would be the bug.
        assert "green" not in styles
        assert "red" not in styles

    def test_unknown_extension_keeps_marker_colours_only(self):
        from novacode_cli.tui.app import NovaApp

        diff = "\n".join(SAMPLE_DIFF)
        text = NovaApp._render_diff_text(diff, path="foo.zzznotalang")
        styles = {str(span.style) for span in text.spans}
        assert not any("#66d9ef" in s for s in styles)
        assert any("on dark_green" in s for s in styles)
        assert any("on dark_red" in s for s in styles)

    def test_plain_text_preserved(self):
        from novacode_cli.tui.app import NovaApp

        diff = "\n".join(SAMPLE_DIFF)
        text = NovaApp._render_diff_text(diff, path="foo.py")
        assert text.plain == diff + "\n"
