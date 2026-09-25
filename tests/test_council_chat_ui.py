"""The council web UI must recover from an error, not wedge.

Reported symptom: "when I do a follow up chat with the councils, nothing happens
in the UI". convene() bails out early while an EventSource is still open
(`if (!topic || es) return;`), and only endRun() clears it. The error handler
used to call setRun(false) instead — which repaints the badge but leaves `es`
open and the composer disabled, so every later question silently did nothing.
"""

from __future__ import annotations

import re

from novacode_cli.commands.chat_handler import _make_chat_html


def _handler_body(event: str) -> str:
    """The JS body registered for one SSE event."""
    html = _make_chat_html()
    match = re.search(
        rf"addEventListener\('{event}',(.*?)\n  \}}\);", html, re.DOTALL
    ) or re.search(rf"addEventListener\('{event}', e => \{{(.*?)\}}\);", html, re.DOTALL)
    assert match, f"no handler found for {event}"
    return match.group(1)


def test_an_error_ends_the_run_so_the_next_question_can_be_asked():
    body = _handler_body("council_error")
    assert "endRun()" in body, "an error must close the stream and re-enable input"


def test_done_still_ends_the_run():
    assert "endRun()" in _handler_body("done")


def test_convene_is_guarded_by_the_open_stream():
    """The guard is why a stale `es` wedges the UI — keep them in sync."""
    html = _make_chat_html()
    assert "if (!topic || es) return;" in html
    assert "if (es) { es.close(); es = null; }" in html, "endRun must null out es"
