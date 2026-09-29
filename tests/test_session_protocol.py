"""Wire-protocol round-trip for parallel-session events.

A spawned session streams ``ui_events`` dataclasses to its parent as JSONL; the
parent decodes them back into the SAME dataclasses and feeds the existing
renderer. That only works if encode->JSON->decode is lossless for every event
type, so these tests parametrize over every dataclass in ``ui_events`` rather
than a hand-picked few — a newly added event type fails here until it's covered.

Three fields get special handling and have dedicated tests: ``FileOp.record``
(a dataclass holding a Path and nested metrics), ``Error.exception`` (dropped),
and ``InterruptRequest.future`` (never an event on the wire).

Runnable directly (``python tests/test_session_protocol.py``) or via pytest.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.sessions import protocol as p

# Every event class the protocol claims to support.
_EVENT_NAMES = sorted(p._EVENT_CLASSES)


def _roundtrip(event):
    """encode -> JSON text -> parse -> decode, i.e. exactly the real path."""
    line = p.dumps({"t": "ev", **p.encode_event(event)})
    parsed = p.loads(line)
    assert parsed is not None
    return p.decode_event(parsed)


def _sample(name: str):
    """Build a populated instance of event class *name*."""
    cls = p._EVENT_CLASSES[name]
    special = {
        "StatusUpdate": lambda: ev.StatusUpdate(message="working"),
        "TextDelta": lambda: ev.TextDelta(text="hello "),
        "ReasoningDelta": lambda: ev.ReasoningDelta(text="thinking"),
        "AssistantMessage": lambda: ev.AssistantMessage(
            text="done", agent_name="nova", agent_color="cyan", is_subagent=True
        ),
        "ToolCall": lambda: ev.ToolCall(
            name="shell", display_str="ls -la", icon="⚡",
            is_main_agent=False, args={"command": "ls", "n": 3}, call_id="t1",
        ),
        "ToolResult": lambda: ev.ToolResult(
            preview="ok", is_error=True, full_output="line\nline", call_id="t1"
        ),
        "TodoUpdate": lambda: ev.TodoUpdate(
            todos=[{"content": "a", "status": "pending"}], agent_name="nova"
        ),
        "ErrorOutput": lambda: ev.ErrorOutput(text="boom"),
        "ContextOverflow": lambda: ev.ContextOverflow(message="prompt is too long"),
        "SubagentActivity": lambda: ev.SubagentActivity(
            kind="dispatched", subagent_type="code-explorer",
            message="m", detail="d", color="green", call_id="t9",
        ),
        "SubagentTask": lambda: ev.SubagentTask(
            task_id="ptc_task_ab12cd34",
            status="failed",
            phase_id="call_eval_1",
            phase_kind="eval",
            subagent_type="researcher",
            label="research: docling",
            description="research docling",
            started_at=1000.0,
            duration_ms=33600,
            error="'charmap' codec can't decode byte 0x8f",
            model="deepseek-v4.1-flash",
        ),
        "UsageUpdate": lambda: ev.UsageUpdate(
            input_tokens=10, output_tokens=2, cache_read_tokens=1, cache_creation_tokens=4
        ),
        "Done": lambda: ev.Done(had_response=True),
        "ContextMessage": lambda: ev.ContextMessage(
            message="review done", event_type="nova_review_complete", icon="✓", color="green"
        ),
        "Error": lambda: ev.Error(message="rate limited", is_provider_notice=True),
        "FileOp": lambda: ev.FileOp(record=_record(), full_output="body", call_id="t2"),
    }
    if name in special:
        return special[name]()
    return cls()  # no-field markers: TextDiscard, CompactionNotice, Cancelled


def _record():
    from novacode_cli.file_ops import FileOperationRecord, FileOpMetrics

    return FileOperationRecord(
        tool_name="write_file",
        display_path="src/main.py",
        physical_path=Path("/tmp/src/main.py"),
        tool_call_id="t2",
        args={"content": "x"},
        status="success",
        error=None,
        metrics=FileOpMetrics(lines_written=3, lines_added=2, bytes_written=99),
        diff="--- a\n+++ b",
        before_content="a",
        after_content="b",
        read_output=None,
        hitl_approved=True,
    )


# ── every event class round-trips ────────────────────────────────────────────


@pytest.mark.parametrize("name", _EVENT_NAMES)
def test_event_roundtrips_unchanged(name):
    original = _sample(name)
    assert _roundtrip(original) == original


def test_registry_covers_ui_events():
    """The registry must be every ui_events dataclass except InterruptRequest."""
    expected = {
        n for n, o in vars(ev).items()
        if isinstance(o, type) and is_dataclass(o) and n != "InterruptRequest"
    }
    assert set(p._EVENT_CLASSES) == expected
    assert "InterruptRequest" not in p._EVENT_CLASSES


# ── the three special cases ──────────────────────────────────────────────────


def test_fileop_record_survives_with_path_and_metrics():
    out = _roundtrip(ev.FileOp(record=_record(), full_output="body", call_id="t2"))
    rec = out.record
    # These are exactly the attributes the TUI renderer reads.
    assert rec.status == "success"
    assert rec.error is None
    assert rec.diff == "--- a\n+++ b"
    assert rec.display_path == "src/main.py"
    assert isinstance(rec.physical_path, Path)
    assert rec.metrics.lines_added == 2
    assert rec.metrics.bytes_written == 99
    assert rec.hitl_approved is True


def test_fileop_with_no_record_is_fine():
    out = _roundtrip(ev.FileOp(record=None, full_output="", call_id=None))
    assert out.record is None


def test_error_drops_exception_but_keeps_message_and_notice():
    original = ev.Error(
        message="usage limit reached", exception=RuntimeError("boom"), is_provider_notice=True
    )
    out = _roundtrip(original)
    assert out.message == "usage limit reached"
    assert out.is_provider_notice is True
    assert out.exception is None  # not serializable; styling rides on the flag


def test_interrupt_request_is_refused():
    # `future` is left None: encoding must refuse on the class alone, before it
    # ever looks at the field (and a bare asyncio.Future() outside a loop warns).
    req = ev.InterruptRequest(kind="tool", payload={}, future=None)
    with pytest.raises(ValueError, match="not encodable"):
        p.encode_event(req)


# ── robustness ───────────────────────────────────────────────────────────────


def test_oversized_field_is_clipped():
    huge = "x" * (p.MAX_FIELD * 2)
    out = _roundtrip(ev.ToolResult(preview="ok", full_output=huge))
    assert len(out.full_output) <= p.MAX_FIELD
    assert out.full_output.endswith("[truncated]")


def test_non_json_tool_args_degrade_to_str():
    # Tool args are arbitrary; one odd value must not fail the whole message.
    out = _roundtrip(ev.ToolCall(name="x", display_str="x", icon="i", args={"p": Path("/a/b")}))
    assert isinstance(out.args["p"], str)


def test_unknown_event_class_decodes_to_none():
    # A child from a different Nova build sends something we don't know.
    assert p.decode_event({"c": "SomeFutureEvent", "d": {}}) is None


def test_decode_ignores_unknown_fields():
    # Forward compat the other way: extra fields must not raise.
    msg = {"c": "Done", "d": {"had_response": True, "brand_new_field": 1}}
    assert p.decode_event(msg) == ev.Done(had_response=True)


def test_loads_skips_blank_and_non_json_lines():
    # A library printing to the child's stdout must not kill the stream.
    assert p.loads("") is None
    assert p.loads("   ") is None
    assert p.loads("Traceback (most recent call last):") is None
    assert p.loads("[1, 2]") is None  # valid JSON, wrong shape
    assert p.loads('{"t": "ready"}') == {"t": "ready"}


def test_dumps_is_one_line_and_utf8_safe():
    line = p.dumps({"t": "ev", "d": {"text": "héllo ⚡\nsecond"}})
    assert line.endswith("\n")
    assert line.count("\n") == 1  # embedded newline must be escaped, not raw
    assert "⚡" in line  # ensure_ascii=False keeps glyphs readable
    assert json.loads(line)["d"]["text"] == "héllo ⚡\nsecond"


def test_cyclic_args_do_not_recurse_forever():
    cyclic: dict = {}
    cyclic["self"] = cyclic
    out = _roundtrip(ev.ToolCall(name="x", display_str="x", icon="i", args=cyclic))
    assert out.args  # produced something rather than blowing the stack


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))
