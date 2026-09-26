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


# ---------------------------------------------------------------------------
# The status-bar null-deref that wedged the composer
# ---------------------------------------------------------------------------
#
# Second, independent cause of "a follow-up question does nothing", found in a
# live browser session after the endRun() fix above had already shipped.
#
# `#status-bar` is a flex row of child spans (#conn, #stat-run, .spacer, .hint,
# #clock). `setStatus()` used to assign `statusBar.textContent`, which DELETES
# every one of those children. From the first status update onward `stat-run`
# was null, so `setRun()` threw a TypeError — and because `convene()` calls
# `setRun(true)` *before* `new EventSource(...)`, every follow-up died before
# its stream was ever created: the typed text was already cleared, the composer
# stayed disabled, and nothing rendered. Permanently.
#
# The tests above could not catch this: they grep the HTML for the string
# "endRun()", and `endRun()` was present and correct — it was `setRun()` inside
# it that threw.


def test_status_message_is_written_to_a_child_not_the_bar():
    """The defect itself: overwriting the bar destroys #stat-run and #clock."""
    html = _make_chat_html()
    body = re.search(r"function setStatus\(m\) \{ (.*?) \}", html)
    assert body, "no setStatus() found"
    assert "textContent" in body.group(1)
    assert "statusBar.textContent" not in body.group(1), (
        "setStatus must not assign the bar's textContent — it deletes the bar's "
        "child spans, which is what wedged the composer on every follow-up"
    )


def test_status_bar_has_a_dedicated_message_child():
    """The message needs its own element, or it can only be shown by wiping the bar."""
    html = _make_chat_html()
    bar = re.search(r'<div id="status-bar">(.*?)</div>', html, re.DOTALL)
    assert bar, "no #status-bar found"
    assert 'id="status-msg"' in bar.group(1)
    # And the elements setRun()/setStatus() depend on must live inside the bar.
    for needed in ("stat-run", "conn", "clock"):
        assert f'id="{needed}"' in bar.group(1), f"#{needed} must be a child of the bar"


def test_set_run_is_null_guarded():
    """Belt and braces: a render helper must never be able to wedge the composer.

    This exact call has now thrown twice. Even if the bar's markup regresses,
    a missing #stat-run must not stop the page.
    """
    html = _make_chat_html()
    body = re.search(r"function setRun\(on\) \{(.*?)\n\}", html, re.DOTALL)
    assert body, "no setRun() found"
    assert "if (!sr) return;" in body.group(1), (
        "setRun must bail when #stat-run is missing instead of throwing"
    )


def test_each_round_ends_at_a_user_approval_gate():
    """The council recommends; the user settles. A verdict is not self-approving."""
    html = _make_chat_html()
    verdict = re.search(r"function renderVerdict\(ev\) \{(.*?)\n\}\n", html, re.DOTALL)
    assert verdict, "no renderVerdict() found"
    assert "buildApproval(ev)" in verdict.group(1), (
        "every verdict must end at the approval gate, not at the vote"
    )
    for label in ("Approve", "Request changes", "Reject"):
        assert label in html, f"missing the {label!r} control"


def test_a_decision_is_recorded_and_the_composer_stays_open():
    """Approving settles the round; it must not end the conversation."""
    html = _make_chat_html()
    gate = re.search(r"function buildApproval\(ev\) \{(.*?)\n\}\n", html, re.DOTALL)
    assert gate, "no buildApproval() found"
    body = gate.group(1)
    assert "/api/council/decision" in body, "the decision must be recorded server-side"
    # Approving must NOT refocus/require another question to continue.
    assert "if (decision !== 'approve') { input.focus(); }" in body
