"""The dynamic-subagents panel's data layer: event mapping and row layout.

The three sources disagree about shape (a quickjs stream event, a sync tool call,
an async state dict), so the translation is pure and tested here rather than
through a terminal.
"""

from __future__ import annotations

import math

import pytest

from novacode_cli.core import subagent_tasks as st
from novacode_cli.ui_events import SubagentTask

# ── quickjs custom-stream events ────────────────────────────────────────────


def test_a_start_event_becomes_a_running_row():
    task = st.task_from_custom_event(
        {
            "id": "ptc_task_ab12cd34",
            "type": "subagent",
            "phase": "start",
            "eval_id": "call_eval_1",
            "subagent_type": "researcher",
            "label": "research: docling",
            "description": "research docling",
        },
        now=1000.0,
        model="deepseek-v4.1-flash",
    )

    assert task is not None
    assert task.status == "running"
    assert task.task_id == "ptc_task_ab12cd34"
    assert task.phase_id == "call_eval_1"
    assert task.subagent_type == "researcher"
    assert task.label == "research: docling"
    assert task.started_at == 1000.0
    assert task.duration_ms is None
    assert task.model == "deepseek-v4.1-flash"


def test_complete_and_error_events_carry_the_duration():
    done = st.task_from_custom_event(
        {"id": "t1", "type": "subagent", "phase": "complete", "duration_ms": 38300},
        now=1000.0,
    )
    failed = st.task_from_custom_event(
        {
            "id": "t2",
            "type": "subagent",
            "phase": "error",
            "duration_ms": 33600,
            "error": "'charmap' codec can't decode byte 0x8f in position 3716",
        },
        now=1000.0,
    )

    assert done is not None
    assert done.status == "done"
    assert done.duration_ms == 38300
    assert failed is not None
    assert failed.status == "failed"
    assert "charmap" in failed.error
    # A completion for a dispatch we never saw start still gets an honest TIME,
    # back-dated from the duration the bridge measured.
    assert done.started_at == 1000.0 - 38.3


def test_only_the_bridge_s_own_events_are_translated():
    """The custom stream carries whatever else middleware writes to it."""
    assert st.task_from_custom_event({"type": "council.phase.started"}, now=1.0) is None
    assert st.task_from_custom_event({"type": "subagent"}, now=1.0) is None  # no id/phase
    assert (
        st.task_from_custom_event({"id": "t", "type": "subagent", "phase": "halfway"}, now=1.0)
        is None
    )
    assert st.task_from_custom_event("not a dict", now=1.0) is None
    assert st.task_from_custom_event(None, now=1.0) is None


def test_a_missing_eval_id_leaves_the_row_ungrouped():
    task = st.task_from_custom_event({"id": "t", "type": "subagent", "phase": "start"}, now=5.0)
    assert task is not None
    assert task.phase_id is None


def test_the_event_type_constant_comes_from_the_package_when_present():
    """The literal is a fallback, not a guess: the package's value wins."""
    assert st.quickjs_event_type()
    try:
        from langchain_quickjs._subagent import SUBAGENT_STREAM_EVENT_TYPE
    except ImportError:  # pragma: no cover — the package is optional
        return
    assert st.quickjs_event_type() == SUBAGENT_STREAM_EVENT_TYPE


# ── sync dispatches ─────────────────────────────────────────────────────────


def test_a_sync_dispatch_and_its_completion_share_a_row_id():
    started = st.task_from_sync_dispatch("call_9", "reviewer", "review auth.py", 500.0)
    finished = st.task_from_sync_completion(
        "call_9", now=520.5, ok=True, started_at=500.0, subagent_type="reviewer"
    )

    assert started.task_id == finished.task_id == "call_9"
    assert started.status == "running"
    assert finished.status == "done"
    # No duration in the completion: derived from the start the tracker recorded.
    assert finished.duration_ms == 20500


def test_a_failed_sync_dispatch_is_marked_failed():
    task = st.task_from_sync_completion("c", now=2.0, ok=False, error="boom")
    assert task.status == "failed"
    assert task.error == "boom"


# ── async (remote) tasks ────────────────────────────────────────────────────


def test_async_entries_map_status_and_stay_out_of_the_eval_grouping():
    running = st.task_from_async_entry(
        {"task_id": "abc", "agent_name": "doc-writer", "status": "running"}, now=100.0
    )
    done = st.task_from_async_entry(
        {"task_id": "abc", "agent_name": "doc-writer", "status": "success"}, now=100.0
    )

    assert running is not None
    assert running.status == "running"
    assert running.phase_kind == "async"
    assert running.phase_id == "async"
    # Prefixed, so a remote thread id can never collide with a bridge dispatch id.
    assert running.task_id == "async:abc"
    assert done is not None
    assert done.status == "done"
    assert done.duration_ms is not None


def test_async_entries_tolerate_junk():
    assert st.task_from_async_entry(None, now=1.0) is None
    assert st.task_from_async_entry({"agent_name": "x"}, now=1.0) is None
    # An unparseable timestamp must not lose the row.
    task = st.task_from_async_entry(
        {"task_id": "t", "status": "running", "created_at": "not-a-time"}, now=42.0
    )
    assert task is not None
    assert task.started_at == 42.0
    iso = st.task_from_async_entry(
        {"task_id": "t", "status": "running", "created_at": "2026-09-29T00:30:00+00:00"},
        now=1.0,
    )
    assert iso is not None
    assert iso.started_at > 0


# ── header, rows, columns ───────────────────────────────────────────────────


def _task(**kwargs: object) -> SubagentTask:
    base: dict[str, object] = {
        "task_id": "t",
        "status": "running",
        "label": "research: docling",
        "started_at": 100.0,
    }
    base.update(kwargs)
    return SubagentTask(**base)  # type: ignore[arg-type]


def test_the_summary_counts_done_phases_and_failures():
    tasks = [
        _task(task_id="1", status="done", phase_id="p1"),
        _task(task_id="2", status="running", phase_id="p1"),
        _task(task_id="3", status="failed", phase_id="p2"),
    ]
    assert st.phase_summary(tasks) == (1, 3, 2, 1)

    title = st.panel_title(tasks)
    assert "1/3 done" in title.plain
    assert "2 phases" in title.plain
    assert "1 failed" in title.plain


def test_the_summary_says_one_phase_not_one_phases():
    title = st.panel_title([_task(phase_id="p1")])
    assert "1 phase" in title.plain
    assert "1 phases" not in title.plain


def test_the_summary_of_nothing_is_all_zeros():
    assert st.phase_summary([]) == (0, 0, 0, 0)
    assert "0/0 done" in st.panel_title([]).plain


def test_rows_and_the_header_are_exactly_the_pane_width():
    """Both ends: a ragged column is as wrong as an overlong one."""
    for width in (40, 60, 80, 120):
        task = _task()
        row = st.task_row(task, width=width, now=101.0)
        header = st.table_header(width)
        assert len(row.plain) == width, (width, row.plain)
        assert len(header.plain) == width, (width, header.plain)


def test_every_panel_line_is_exactly_the_panel_width():
    """The composed two-pane body has to tile the width, at every size."""
    tasks = [
        _task(task_id="1", status="done", phase_id="phase-aaa", duration_ms=38_300),
        _task(task_id="2", status="running", phase_id="phase-aaa", started_at=100.0),
        _task(
            task_id="3",
            status="failed",
            phase_id="phase-bbb",
            error="'charmap' codec can't decode byte 0x8f in position 3716",
            duration_ms=33_600,
        ),
    ]
    for width in (40, 59, 60, 80, 120, 200):
        lines = st.panel_lines(tasks, width=width, now=108.3)
        assert lines, width
        for line in lines:
            assert len(line.plain) == width, (width, line.plain)

    wide = [line.plain for line in st.panel_lines(tasks, width=100, now=108.3)]
    assert "Phases" in wide[0]
    assert "TASK" in wide[0]
    assert "MODEL" in wide[0]
    assert "TIME" in wide[0]
    # The pane is dropped on a narrow terminal rather than both being squeezed.
    narrow = [line.plain for line in st.panel_lines(tasks, width=50, now=108.3)]
    assert all("Phases" not in line for line in narrow)


def test_the_panel_shows_the_failure_and_the_two_phases():
    tasks = [
        _task(task_id="1", status="done", phase_id="aaa1", duration_ms=38_300),
        _task(task_id="2", status="running", phase_id="aaa1", started_at=100.0),
        _task(
            task_id="3",
            status="failed",
            phase_id="bbb2",
            label="research: chroma",
            error="charmap",
            duration_ms=33_600,
        ),
    ]
    body = "\n".join(line.plain for line in st.panel_lines(tasks, width=100, now=200.0))
    assert "charmap" in body
    assert "✗" in body
    assert "✓" in body
    # Each phase reports its own progress: the two-task phase is 1/2, the
    # single failed one 0/1.
    assert "1/2" in body
    assert "0/1" in body


def test_a_long_name_is_ellipsised_and_a_short_one_padded():
    short = st.task_row(_task(label="x"), width=80, now=101.0)
    long = st.task_row(_task(label="y" * 200), width=80, now=101.0)
    assert "…" not in short.plain
    assert "y…" in long.plain
    assert len(long.plain) == 80


def test_a_failed_row_carries_its_error_text():
    failed = _task(
        status="failed",
        subagent_type="researcher",
        label="research: chroma",
        error="'charmap' codec can't decode byte 0x8f",
        duration_ms=33600,
    )
    row = st.task_row(failed, width=110, now=999.0)
    assert "✗" in row.plain
    # The label is shown as given: it already opens with its own kind prefix
    # ("research: chroma"), and a matching subagent type on top of one would read as a
    # duplicate — see subagent_tasks.task_row. Earlier this expected the type to be
    # prefixed anyway, which the renderer has not done since it learned to detect one.
    assert "research: chroma" in row.plain
    assert "researcher: research: chroma" not in row.plain
    assert "charmap" in row.plain
    assert "33.6s" in row.plain


def test_glyphs_and_the_model_column():
    running = st.task_row(_task(), width=80, now=101.0)
    done = st.task_row(_task(status="done", duration_ms=38300), width=80, now=101.0)
    assert "●" in running.plain
    assert "Active subagents" in st.panel_title([_task()]).plain
    assert "✓" in done.plain
    assert "—" in running.plain  # an unknown model is shown, not left blank


def test_a_finished_rows_time_uses_its_duration_and_a_running_one_ages():
    finished = _task(status="done", duration_ms=38300, started_at=1.0)
    running = _task(started_at=100.0)
    assert st.elapsed_for(finished, now=9_999.0) == 38.3
    assert st.elapsed_for(running, now=100.0) == 0.0
    assert st.elapsed_for(running, now=138.3) == pytest.approx(38.3)
    # A row with neither start nor duration is 0, never negative or NaN.
    assert st.elapsed_for(_task(started_at=0.0), now=500.0) == 0.0


def test_elapsed_formatting_at_the_boundary():
    assert st.format_elapsed(38.34) == "38.3s"
    assert st.format_elapsed(60) == "1m 00s"
    assert st.format_elapsed(64.4) == "1m 04s"
    assert st.format_elapsed(3600) == "60m 00s"
    assert st.format_elapsed(-1) == "—"
    assert st.format_elapsed(math.nan) == "—"


def test_the_phases_pane_shows_progress_and_elapsed():
    tasks = [
        _task(task_id="1", status="done", duration_ms=30_000, phase_id="p1"),
        _task(task_id="2", status="running", started_at=100.0, phase_id="p1"),
    ]
    row = st.phase_row("1", tasks, now=108.3)
    assert "1/2" in row.plain
    assert "38.3s" in row.plain  # 30s finished + 8.3s running


# ── preview: what one row is doing ──────────────────────────────────────────


def test_a_click_can_tell_a_task_from_the_phase_beside_it():
    rows = [SubagentTask(task_id="a", phase_id="p1"), SubagentTask(task_id="b", phase_id="p1")]
    wide = st.panel_body(rows, width=100, now=0.0)
    # Line 1 holds phase p1 on the left and task "a" on the right.
    assert (wide.phase_at(1), wide.task_at(1)) == ("p1", "a")
    assert wide.task_at(2) == "b"
    assert wide.task_column == st.PHASE_COLUMN + 1
    assert st.panel_body(rows, width=50, now=0.0).task_column == 0


def test_preview_joins_streamed_text_and_stays_bounded():
    entries: list[tuple[str, str]] = []
    for kind, text in [("text", "Read"), ("text", "ing."), ("tool", "grep(x)"), ("error", "✗ no")]:
        st.append_preview(entries, kind, text)
    assert entries == [("text", "Reading."), ("tool", "grep(x)"), ("error", "✗ no")]
    for i in range(st.MAX_PREVIEW_ENTRIES + 50):
        st.append_preview(entries, "tool", str(i))
    assert len(entries) == st.MAX_PREVIEW_ENTRIES


def test_preview_renders_markdown_and_hangs_results_under_their_tool():
    from rich.console import Console

    entries = [
        ("text", "## Findings\n\n- **two** hits\n"),
        ("tool", "grep(x)"),
        ("result", "✓ 2 matches"),
        ("error", "✗ no such file"),
        ("text", "Done."),
    ]
    cache: dict = {}
    console = Console(width=60, record=True, color_system=None)
    console.print(st.preview_renderable(entries, cache=cache))
    lines = [line.rstrip() for line in console.export_text().splitlines()]

    # Markdown is rendered, not shown as source.
    assert not any("##" in line or "**" in line for line in lines)
    assert any("Findings" in line for line in lines)
    assert any("two hits" in line for line in lines)
    # A tool call, its results beneath it, and a gap either side of the run.
    at = lines.index("▸ grep(x)")
    assert lines[at - 1] == ""
    assert lines[at + 1 : at + 4] == ["  ⎿ ✓ 2 matches", "  ⎿ ✗ no such file", ""]
    assert lines[at + 4] == "Done."
    # Unchanged entries are not rebuilt on the next update.
    first = cache[entries[0]]
    st.preview_renderable([*entries, ("tool", "ls()")], cache=cache)
    assert cache[entries[0]] is first


def test_a_remote_thread_becomes_the_same_preview():
    entries = st.remote_preview(
        [
            {"type": "human", "content": "audit deps"},
            {
                "type": "ai",
                "content": [{"type": "text", "text": "Checking."}],
                "tool_calls": [{"name": "grep", "args": {"pattern": "requests"}, "id": "c1"}],
            },
            {"type": "tool", "name": "grep", "content": "Error: bad pattern", "status": "error"},
            "not a message",
        ]
    )
    assert [kind for kind, _ in entries] == ["result", "text", "tool", "error"]
    assert entries[0][1] == "task: audit deps"
    assert "grep" in entries[2][1]
    assert st.remote_preview(None) == []
