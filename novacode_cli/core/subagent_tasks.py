"""Row data for the dynamic-subagents panel: pure mapping and formatting.

Three sources feed the panel and they disagree about shape, so the translation
lives here rather than in the renderer:

* ``langchain-quickjs`` publishes a ``subagent`` event per ``task()`` dispatch on
  LangGraph's ``custom`` stream (start / complete / error, with ``id``,
  ``eval_id``, ``subagent_type``, ``label``, ``description``, ``duration_ms`` and
  ``error``). One fan-out shares an ``eval_id``, which is what the panel shows as
  a phase.
* The sync ``task`` tool is tracked by :class:`SubagentTracker`, which records a
  start time per dispatch.
* Async (remote) tasks come from graph state as plain dicts with a status and an
  agent name, and no duration at all.

Everything here is pure: no Textual, no live app, so the column layout and the
event mapping can be tested without a terminal.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

if TYPE_CHECKING:
    from novacode_cli.ui_events import SubagentTask

#: The discriminator the quickjs bridge stamps on its fan-out events
#: (``langchain_quickjs._subagent.SUBAGENT_STREAM_EVENT_TYPE``). Compared as a
#: literal so this module never needs the package imported to be testable.
CUSTOM_EVENT_TYPE = "subagent"

#: The phase id async (remote) tasks are grouped under.
ASYNC_PHASE = "async"

#: How a row was launched. Drives the phase grouping's fallback label.
PHASE_KINDS = ("eval", "direct", "async")

#: Status glyphs, matching the vocabulary the rest of the TUI uses (✓ / ✗ from
#: the tool cards, the hourglass from a running tool).
STATUS_GLYPH = {"running": "⏳", "done": "✓", "failed": "✗"}

#: Width of the phases pane, and the caps that keep a 32-way fan-out readable.
PHASE_COLUMN = 18
MODEL_COLUMN = 18
TIME_COLUMN = 8
MAX_ROWS = 200
MAX_PHASES = 20

#: Layout thresholds, named so the intent survives a later tweak.
WIDE_PANEL = 70  # a pane this wide can afford a real model column
TWO_PANE_WIDTH = 60  # below this the phases pane is dropped instead of squeezed
SECONDS_PER_MINUTE = 60


def quickjs_event_type() -> str:
    """The bridge's own discriminator, if the package is importable.

    Falls back to the literal so the panel keeps working when
    ``langchain-quickjs`` is missing or has moved its constant.
    """
    try:
        from langchain_quickjs._subagent import (  # type: ignore[import-not-found]
            SUBAGENT_STREAM_EVENT_TYPE,
        )
    except Exception:  # noqa: BLE001 — feature detection, never a failure
        return CUSTOM_EVENT_TYPE
    return str(SUBAGENT_STREAM_EVENT_TYPE)


def format_elapsed(seconds: float) -> str:
    """``38.3s`` / ``1m 04s``, the TIME column."""
    if seconds < 0 or math.isnan(seconds):  # negative or NaN
        return "—"
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), SECONDS_PER_MINUTE)
    return f"{minutes}m {rest:02d}s"


def elapsed_for(task: SubagentTask, now: float | None = None) -> float:
    """Seconds a task took, or has been running for.

    A finished row uses the duration its completing event carried, so the column
    stops moving and matches the bridge's own measurement. A running row ages
    against *now*.
    """
    if task.duration_ms is not None:
        return task.duration_ms / 1000.0
    if not task.started_at:
        return 0.0
    return max(0.0, (now if now is not None else time.time()) - task.started_at)


def status_glyph(status: str) -> str:
    """The single-cell mark in front of a row."""
    return STATUS_GLYPH.get(status, "•")


def task_from_custom_event(
    payload: object, *, now: float, model: str | None = None
) -> SubagentTask | None:
    """Translate one quickjs ``subagent`` stream event, or ``None`` for anything else.

    Returning ``None`` for unknown payloads matters: the custom stream carries
    whatever else middleware writes to it, and the loop forwards all of it.
    """
    from novacode_cli.ui_events import SubagentTask

    if not isinstance(payload, dict) or payload.get("type") != quickjs_event_type():
        return None
    task_id = str(payload.get("id") or "")
    status = {"start": "running", "complete": "done", "error": "failed"}.get(
        str(payload.get("phase") or "")
    )
    if not task_id or status is None:
        return None

    raw_duration = payload.get("duration_ms")
    duration_ms = int(raw_duration) if isinstance(raw_duration, (int, float)) else None
    if status == "running":
        started_at = now
    elif duration_ms is not None:
        # A completion for a dispatch we never saw start (a fan-out already in
        # flight when the panel appeared): back-date it so TIME is still right.
        started_at = now - duration_ms / 1000.0
    else:
        started_at = 0.0

    eval_id = payload.get("eval_id")
    return SubagentTask(
        task_id=task_id,
        status=status,
        phase_id=str(eval_id) if eval_id else None,
        phase_kind="eval",
        subagent_type=str(payload.get("subagent_type") or ""),
        label=str(payload.get("label") or ""),
        description=str(payload.get("description") or ""),
        started_at=started_at,
        duration_ms=duration_ms,
        error=str(payload.get("error") or ""),
        model=model,
    )


def task_from_sync_dispatch(
    call_id: str,
    subagent_type: str,
    description: str,
    started_at: float,
    *,
    model: str | None = None,
) -> SubagentTask:
    """A dispatch through the blocking ``task`` tool."""
    from novacode_cli.ui_events import SubagentTask

    return SubagentTask(
        task_id=call_id,
        status="running",
        phase_kind="direct",
        subagent_type=subagent_type,
        label=description,
        description=description,
        started_at=started_at,
        model=model,
    )


def task_from_sync_completion(
    call_id: str,
    *,
    now: float,
    ok: bool,
    duration_ms: int | None = None,
    error: str = "",
    started_at: float = 0.0,
    subagent_type: str = "",
    description: str = "",
    model: str | None = None,
) -> SubagentTask:
    """The completing half of a blocking ``task`` call."""
    from novacode_cli.ui_events import SubagentTask

    resolved = duration_ms
    if resolved is None and started_at:
        resolved = int(max(0.0, now - started_at) * 1000)
    return SubagentTask(
        task_id=call_id,
        status="done" if ok else "failed",
        phase_kind="direct",
        subagent_type=subagent_type,
        label=description,
        description=description,
        started_at=started_at,
        duration_ms=resolved,
        error=error,
        model=model,
    )


#: Async task statuses, as the Agent Protocol reports them.
_ASYNC_STATUS = {
    "running": "running",
    "pending": "running",
    "queued": "running",
    "success": "done",
    "completed": "done",
    "done": "done",
    "failed": "failed",
    "error": "failed",
    "cancelled": "failed",
}


def task_from_async_entry(
    entry: object, *, now: float, model: str | None = None
) -> SubagentTask | None:
    """A remote task from the graph's ``async_tasks`` state."""
    from novacode_cli.ui_events import SubagentTask

    if not isinstance(entry, dict):
        return None
    task_id = str(entry.get("task_id") or entry.get("thread_id") or "")
    if not task_id:
        return None
    status = _ASYNC_STATUS.get(str(entry.get("status") or "").lower(), "running")
    started_at = _parse_timestamp(entry.get("created_at")) or now
    return SubagentTask(
        task_id=f"async:{task_id}",
        status=status,
        phase_id="async",
        phase_kind="async",
        subagent_type=str(entry.get("agent_name") or "async"),
        label=str(entry.get("agent_name") or task_id),
        description=str(entry.get("thread_id") or ""),
        started_at=started_at,
        duration_ms=int(max(0.0, now - started_at) * 1000) if status != "running" else None,
        model=model,
    )


def _parse_timestamp(value: object) -> float | None:
    """Best-effort epoch seconds from an ISO string or a number."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except Exception:  # noqa: BLE001 — a bad timestamp must not lose the row
        return None


def phase_summary(tasks: list[SubagentTask]) -> tuple[int, int, int, int]:
    """``(done, total, phases, failed)`` for the panel's header."""
    done = sum(1 for task in tasks if task.status == "done")
    failed = sum(1 for task in tasks if task.status == "failed")
    phases = len({task.phase_id for task in tasks if task.phase_id}) or (1 if tasks else 0)
    return done, len(tasks), phases, failed


def panel_title(tasks: list[SubagentTask], *, collapsed: bool = False) -> Text:
    """The header line: caret, name, then the counts."""
    done, total, phases, failed = phase_summary(tasks)
    title = Text()
    title.append("▶ " if collapsed else "▼ ")
    title.append("dynamic subagents", style="bold")
    title.append("   ")
    title.append(f"{done}/{total} done", style="dim")
    title.append(" · ", style="dim")
    title.append(f"{phases} phase" + ("s" if phases != 1 else ""), style="dim")
    if failed:
        title.append(" · ", style="dim")
        title.append(f"{failed} failed", style="bold red")
    return title


def column_widths(width: int) -> tuple[int, int, int]:
    """``(name, model, time)`` widths for a task row *width* cells wide.

    The row spends six cells on layout — the glyph, a space, and two two-space
    separators — so the name column absorbs the remainder. Getting this wrong is
    an invisible bug until a row and the header no longer line up.
    """
    overhead = 6 + TIME_COLUMN
    model_width = MODEL_COLUMN if width >= WIDE_PANEL else max(8, width // 5)
    name_width = max(12, width - overhead - model_width)
    return name_width, model_width, TIME_COLUMN


def task_row(
    task: SubagentTask,
    *,
    width: int,
    now: float,
    selected: bool = False,
) -> Text:
    """One table row: glyph, task name (with its error), model, time."""
    name_width, model_width, _time_width = column_widths(width)
    name = task.label or task.description or task.subagent_type or task.task_id
    if task.subagent_type and not name.startswith(f"{task.subagent_type}:"):
        name = f"{task.subagent_type}: {name}"
    if task.status == "failed" and task.error:
        # The failure text is the whole point of the row, so it gets the space
        # left over rather than the name being truncated to make room for it.
        name = f"{name} — {task.error}"

    style = {"done": "dim", "failed": "red", "running": ""}.get(task.status, "")
    row = Text(style=style)
    row.append(status_glyph(task.status) + " ")
    row.append(_fit(name, name_width))
    row.append("  ")
    row.append(_fit(task.model or "—", model_width), style="dim")
    row.append("  ")
    row.append(format_elapsed(elapsed_for(task, now)).rjust(TIME_COLUMN), style="dim")
    if selected:
        row.stylize("reverse")
    return row


def phase_row(
    label: str,
    tasks: list[SubagentTask],
    *,
    now: float,
    expanded: bool = True,
    selected: bool = False,
) -> Text:
    """One entry in the phases pane: caret, label, progress, elapsed."""
    done, total, _phases, _failed = phase_summary(tasks)
    row = Text()
    row.append("▾ " if expanded else "▸ ")
    row.append(f"{label} ", style="bold")
    row.append(f"{done}/{total}", style="dim")
    row.append(" · ", style="dim")
    row.append(format_elapsed(sum(elapsed_for(task, now) for task in tasks)), style="dim")
    if selected:
        row.stylize("reverse")
    return row


def table_header(width: int) -> Text:
    """The ``TASK / MODEL / TIME`` header, aligned with :func:`task_row`.

    Indented two cells to sit over the name column, since a task row spends its
    first two on the status glyph and a space.
    """
    name_width, model_width, _time_width = column_widths(width)
    header = Text(style="dim")
    header.append("  ")
    header.append("TASK".ljust(name_width))
    header.append("  ")
    header.append("MODEL".ljust(model_width))
    header.append("  ")
    header.append("TIME".rjust(TIME_COLUMN))
    return header


@dataclass
class PanelBody:
    """A rendered panel, plus what each line belongs to.

    The mapping is what makes the panel clickable: a click arrives as a line
    offset, so the widget needs to know whether that line is a phase (toggle it)
    or a task (select it).
    """

    lines: list[Text]
    row_phases: list[str | None]
    row_tasks: list[str | None]

    def phase_at(self, index: int) -> str | None:
        """The phase for line *index*, or ``None`` if it is not a phase row."""
        if 0 <= index < len(self.row_phases):
            return self.row_phases[index]
        return None

    def task_at(self, index: int) -> str | None:
        """The task id for line *index*, or ``None``."""
        if 0 <= index < len(self.row_tasks):
            return self.row_tasks[index]
        return None


def panel_body(
    tasks: list[SubagentTask],
    *,
    width: int,
    now: float,
    phase_order: list[str] | None = None,
    collapsed_phases: frozenset[str] | set[str] = frozenset(),
    selected: str | None = None,
) -> PanelBody:
    """The whole panel body: the phases pane beside the task table.

    Two panes, not one list, because they answer different questions: the left
    says which fan-out a task belongs to and how that fan-out is doing, the right
    is the per-task detail. Below ~60 cells the pane is dropped so the table keeps
    its columns rather than both being squeezed.
    """
    left_width = PHASE_COLUMN if width >= TWO_PANE_WIDTH else 0
    right_width = width - left_width - 1 if left_width else width

    left: list[Text | None] = []
    left_phases: list[str | None] = []
    if left_width:
        left.append(Text("Phases", style="dim"))
        left_phases.append(None)
        for position, phase_id in enumerate(_ordered_phases(tasks, phase_order), start=1):
            members = [task for task in tasks if task.phase_id == phase_id]
            collapsed = phase_id in collapsed_phases
            left.append(
                phase_row(
                    # Numbered, in arrival order: an eval call id's tail ("al_1")
                    # is not something to read a panel by.
                    "async" if phase_id == ASYNC_PHASE else str(position),
                    members,
                    now=now,
                    expanded=not collapsed,
                    selected=phase_id == selected,
                )
            )
            left_phases.append(phase_id)

    right: list[Text] = [table_header(right_width)]
    right_tasks: list[str | None] = [None]
    for task in tasks:
        if task.phase_id in collapsed_phases:
            continue
        right.append(task_row(task, width=right_width, now=now, selected=task.task_id == selected))
        right_tasks.append(task.task_id)

    lines: list[Text] = []
    row_phases: list[str | None] = []
    row_tasks: list[str | None] = []
    for index in range(max(len(left), len(right))):
        line = Text()
        if left_width:
            pane = left[index] if index < len(left) else None
            # A line can only be one thing: the phase pane wins, because that is
            # the row the user aimed at.
            row_phases.append(left_phases[index] if index < len(left_phases) else None)
            row_tasks.append(None)
            line.append_text(_pad(pane, left_width))
            line.append("│", style="dim")
        else:
            row_phases.append(None)
            row_tasks.append(right_tasks[index] if index < len(right_tasks) else None)
        if index < len(right):
            line.append_text(right[index])
        lines.append(line)
    return PanelBody(lines=lines, row_phases=row_phases, row_tasks=row_tasks)


def panel_lines(
    tasks: list[SubagentTask],
    *,
    width: int,
    now: float,
    phase_order: list[str] | None = None,
    selected: str | None = None,
) -> list[Text]:
    """Just the rendered lines of :func:`panel_body`."""
    return panel_body(tasks, width=width, now=now, phase_order=phase_order, selected=selected).lines


def _ordered_phases(tasks: list[SubagentTask], phase_order: list[str] | None) -> list[str]:
    """Phase ids in the order they first appeared, or the caller's order."""
    if phase_order is not None:
        return [phase_id for phase_id in phase_order if phase_id][:MAX_PHASES]
    # dict.fromkeys, not a set: the order a fan-out's phases arrived in is the
    # order the user watched them start.
    return list(dict.fromkeys(task.phase_id for task in tasks if task.phase_id))[:MAX_PHASES]


def _pad(text: Text | None, width: int) -> Text:
    """Exactly *width* cells: padded, or ellipsised if the text overruns."""
    if text is None:
        return Text(" " * width)
    plain = text.plain
    if len(plain) == width:
        return text
    if len(plain) < width:
        padded = Text()
        padded.append_text(text)
        padded.append(" " * (width - len(plain)))
        return padded
    clipped = Text()
    clipped.append_text(text)
    clipped.truncate(width - 1, overflow="ellipsis")
    return clipped


def _fit(text: str, width: int) -> str:
    """Pad or ellipsise to exactly *width* cells (single-line)."""
    flat = " ".join(str(text).split())
    if len(flat) <= width:
        return flat.ljust(width)
    if width <= 1:
        return "…"[:width]
    return flat[: width - 1] + "…"
