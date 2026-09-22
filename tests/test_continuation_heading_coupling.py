"""The resume-briefing leak guard depends on EXACT heading strings.

``core/streaming.py`` suppresses the resume briefing when a model echoes it back
to the user, matching continuation headings by exact string
(``_CONTINUATION_SECTIONS``). So annotating a heading in
``session_prompt_builder`` — however well-intentioned — silently stops the guard
from recognising a real briefing, and the whole prompt leaks into the transcript.

The existing leak tests feed the OLD literal heading, so they keep passing while
the real one drifts. These bind the builder's output to the guard directly.
"""

from __future__ import annotations

from novacode_cli.core.streaming import (
    _CONTINUATION_SECTIONS,
    _HEADING_RE,
    is_internal_context_text,
    looks_like_continuation_briefing,
)
from novacode_cli.session.session_persistence import SessionData, SessionMeta
from novacode_cli.session.session_prompt_builder import build_continuation_prompt


def _session(memory: str | None = "We fixed the glob bug.") -> SessionData:
    return SessionData(
        meta=SessionMeta(
            session_id="s1",
            thread_id="t1",
            created_at="2026-01-01T00:00:00+00:00",
            last_active="2026-01-01T00:00:00+00:00",
            project_root="B:/proj",
            repo_hash="deadbeef",
            Nova_md_checksum="deadbeef",
            model_name="gpt-4o",
            assistant_id="nova-agent",
            message_count=0,
        ),
        memory=memory,
    )


def test_builder_emits_the_exact_session_memory_heading() -> None:
    """Every recognised heading the builder emits must match the guard exactly."""
    messages = build_continuation_prompt(_session(), "BASE PROMPT")
    text = str(messages[0].content)

    headings = {m.group(1).strip().rstrip(":").lower() for m in _HEADING_RE.finditer(text)}
    assert "session memory" in headings, (
        "the Session Memory heading drifted from the exact form the leak guard "
        f"matches; headings found: {sorted(headings)}; "
        f"guard set: {sorted(_CONTINUATION_SECTIONS)}"
    )


def test_annotated_session_memory_heading_would_break_the_guard() -> None:
    """Pins WHY the annotation was reverted — as a live assertion, not a rule."""
    annotated = "## Session Memory (derived summary — may be incomplete)\n\nbody"
    assert not looks_like_continuation_briefing(annotated), (
        "if this now passes, the guard learned prefix matching and the heading "
        "may be safely annotated again"
    )
    assert looks_like_continuation_briefing("## Session Memory\n\nbody")


def test_real_builder_output_is_suppressed_as_a_leak() -> None:
    """End-to-end: the briefing the builder produces must be recognised."""
    messages = build_continuation_prompt(_session(), "BASE PROMPT")
    text = str(messages[0].content)

    assert is_internal_context_text(text), (
        "a restored briefing was not recognised as internal context — it would "
        "leak into the transcript when echoed back"
    )


def test_derived_summary_framing_is_present_in_the_body() -> None:
    """The labelling still reaches the model — in the body, not the heading."""
    messages = build_continuation_prompt(_session(), "BASE PROMPT")
    text = str(messages[0].content)

    assert "derived" in text.lower()
    assert "not a verified record" in text


def test_fresh_session_has_no_memory_block_leak() -> None:
    messages = build_continuation_prompt(_session(memory=None), "BASE PROMPT")
    text = str(messages[0].content)
    assert "fresh start" in text
    assert is_internal_context_text(text)
