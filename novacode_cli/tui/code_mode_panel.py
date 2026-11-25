"""Compact tool rows, with recorded operation summaries for Code Mode runs."""

from __future__ import annotations

import json
import time
from collections import Counter
from contextlib import suppress
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import Collapsible, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.timer import Timer
    from textual.widget import Widget

CODE_MODE_TOOLS = frozenset({"eval", "MCPCode"})
_SHELL_TOOLS = frozenset(
    {
        "shell",
        "bash",
        "execute",
        "execute_bash",
        "run_command",
        "run_tests",
        "start_dev_server",
    }
)
_DIFF_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "multi_edit",
        "str_replace",
        "apply_patch",
    }
)
_MAX_TOOL_NAMES = 3
_PREVIEW_LINES = 3
_PREVIEW_WIDTH = 160
#: A turn start restored from another process is not a duration; ignore it.
_MAX_ELAPSED_SECONDS = 12 * 3600.0


def format_duration(seconds: float) -> str:
    """Compact duration: milliseconds below a second, seconds above."""
    seconds = max(0.0, float(seconds))
    milliseconds = seconds * 1000
    if milliseconds < 1:
        return "<1ms"
    if seconds < 1:
        return f"{round(milliseconds)}ms"
    if seconds < 60:
        rounded = round(seconds, 1)
        if rounded < 60:
            return f"{rounded:.1f}s".replace(".0s", "s")
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest}s"


class ToolTiming:
    """One tool call's timestamps, plus the turn clock the row reports against."""

    def __init__(
        self,
        *,
        turn_started: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool = True,
    ) -> None:
        self._clock = clock
        self._turn_started = turn_started
        self.enabled = enabled
        self.started_at = clock()
        self.ended_at: float | None = None

    @property
    def took(self) -> float | None:
        """Seconds this call has run, or None while it is still running."""
        return None if self.ended_at is None else max(0.0, self.ended_at - self.started_at)

    def elapsed(self) -> float | None:
        """Seconds since the turn started, or None when that is not knowable."""
        if self._turn_started is None or self._turn_started <= 0:
            return None
        span = self._clock() - self._turn_started
        return span if 0 <= span <= _MAX_ELAPSED_SECONDS else None

    def stop(self) -> None:
        """Freeze the end timestamp; a second stop keeps the first one."""
        if self.ended_at is None:
            self.ended_at = self._clock()

    def summary(self) -> str:
        """The row's timing line, omitting whatever is not known yet."""
        if not self.enabled:
            return ""
        parts = []
        took = self.took
        if took is not None:
            parts.append(f"Took {format_duration(took)}")
        elapsed = self.elapsed()
        if elapsed is not None:
            parts.append(f"Elapsed {format_duration(elapsed)}")
        return " · ".join(parts)


def _line(value: str, limit: int = 120) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _operation_name(item: dict) -> str:
    name, server = item.get("tool_name"), item.get("server_name")
    if isinstance(name, str) and name:
        return f"{server}/{name}" if isinstance(server, str) and server else name
    namespace, action = item.get("namespace"), item.get("action")
    return f"{namespace}.{action}" if isinstance(namespace, str) and isinstance(action, str) else ""


def summarize_run(
    output: str, *, is_error: bool, preview: str, fallback: str = "eval"
) -> tuple[str, str, bool]:
    """Summarize reported operations, never infer executed calls from JavaScript."""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        data = None
    return _summarize(data, is_error=is_error, preview=preview, fallback=fallback)


def _cancelled_count(data: object) -> int:
    outcomes = data.get("outcomes") if isinstance(data, dict) else None
    return (
        sum(isinstance(item, dict) and item.get("status") == "cancelled" for item in outcomes)
        if isinstance(outcomes, list)
        else 0
    )


def _summarize(
    data: object, *, is_error: bool, preview: str, fallback: str
) -> tuple[str, str, bool]:
    title = fallback
    failed = is_error
    note = ""
    if isinstance(data, dict):
        outcomes = data.get("outcomes")
        outcomes = (
            [item for item in outcomes if isinstance(item, dict)]
            if isinstance(outcomes, list)
            else []
        )
        names = Counter(_operation_name(item) for item in outcomes)
        names.pop("", None)
        if names:
            shown = [
                f"{_line(name, 40)} x{count}" if count > 1 else _line(name, 40)
                for name, count in list(names.items())[:_MAX_TOOL_NAMES]
            ]
            title = ", ".join(shown)
            if len(names) > _MAX_TOOL_NAMES:
                title += f" +{len(names) - _MAX_TOOL_NAMES} more"
        failures = data.get("failed_calls")
        failed = (
            failed
            or bool(data.get("error"))
            or (type(failures) is int and failures > 0)
            or any(item.get("status") == "failed" for item in outcomes)
        )
        error = data.get("error")
        if isinstance(error, str):
            note = _line(error)
        if not note and failed:
            for item in outcomes:
                error = item.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    note = _line(error["message"])
                    break
        omitted = data.get("outcomes_omitted")
        notes = [note] if note else []
        cancelled = _cancelled_count(data)
        if cancelled:
            noun = "operation" if cancelled == 1 else "operations"
            notes.append(f"{cancelled} {noun} cancelled.")
        if type(omitted) is int and omitted > 0:
            notes.append(f"{omitted} operation records omitted; inspect Full output for details.")
        notes.extend(
            _line(data[key], 400)
            for key in ("hint", "recovery", "notice", "state_warning")
            if isinstance(data.get(key), str) and data[key]
        )
        if data.get("truncated") is True:
            saved = data.get("output_file")
            notes.append(
                f"Complete selected result: {_line(saved['path'], 240)}"
                if isinstance(saved, dict) and isinstance(saved.get("path"), str)
                else "Selected output is truncated; inspect smaller slices before relying on it."
            )
        note = "\n".join(notes)
    if failed and not note:
        note = _line(preview or "The script failed. Expand Full output for details.")
    return title, note, failed


class CodeModePanel(Collapsible):
    """A status row and short result preview, with exact details behind disclosure."""

    DEFAULT_CSS = """
    CodeModePanel {
        height: auto; border: none; padding: 0; margin-bottom: 1;
        background: transparent;
    }
    CodeModePanel:focus-within { background-tint: transparent; }
    CodeModePanel .code-mode-heading { height: 1; }
    CodeModePanel .code-mode-status { width: 2; color: $success; }
    CodeModePanel.tool-active .code-mode-status { color: $warning; }
    CodeModePanel.failed .code-mode-status { color: $error; }
    CodeModePanel .code-mode-label {
        width: 1fr; color: $text; text-wrap: nowrap; text-overflow: ellipsis;
    }
    /* Timing sits at the right edge so a long tool label ellipsizes first. */
    CodeModePanel .code-mode-timing {
        width: auto; color: $text-muted; text-style: none; text-align: right;
    }
    CodeModePanel > CollapsibleTitle {
        color: $text-muted; padding: 0 0 0 2; text-style: none;
    }
    CodeModePanel.tool > CollapsibleTitle:hover,
    CodeModePanel.tool > CollapsibleTitle:focus { color: $accent; }
    CodeModePanel > Contents { padding-left: 2; }
    CodeModePanel .code-mode-note { height: auto; padding-left: 2; color: $text-muted; }
    CodeModePanel .code-mode-preview {
        height: auto; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    CodeModePanel .code-mode-source, CodeModePanel .code-mode-output { height: auto; }
    CodeModePanel Collapsible {
        border: none; background: transparent; padding: 0;
    }
    CodeModePanel.failed .code-mode-note, CodeModePanel.failed .terminal-log { color: $error; }
    """

    def __init__(
        self,
        code: str,
        *,
        tool_name: str = "eval",
        display_str: str | None = None,
        output_widget: Widget | None = None,
        streaming: bool = False,
        timing: ToolTiming | None = None,
    ) -> None:
        """Keep the exact script and result behind separate disclosure controls."""
        self.status = Static("•", classes="code-mode-status")
        self.heading = Static(
            Text(f"Running {display_str or tool_name}"), classes="code-mode-label"
        )
        self.timing = timing if timing is not None else ToolTiming()
        self.timing_label = Static("", classes="code-mode-timing", markup=False)
        self._ticker: Timer | None = None
        self.preview = Static("", classes="code-mode-preview", markup=False)
        self.preview.display = False
        self.note = Static("", classes="code-mode-note", markup=False)
        self.note.display = False
        self.tool_name = tool_name
        self.display_str = display_str
        self._source_text = code or "(script unavailable)"
        self._output_text = "Waiting for output."
        self.result_preview = ""
        self.source: Static | None = None
        self.output = output_widget
        self._diff_blocks: list[Text] = []
        """Diffs for writes performed *inside* this call (Code Mode's nested
        edits). Kept here because ``finish`` replaces ``_output_text`` with the
        call's own result, and the details are only mounted when expanded."""
        self._source_title = "JavaScript" if display_str is None else "Arguments"
        super().__init__(
            *([output_widget] if streaming and output_widget is not None else []),
            title=f"{self._source_title} & full output (Enter to expand)",
            collapsed=not streaming,
            collapsed_symbol="",
            expanded_symbol="",
            classes="tool-active",
        )

    @classmethod
    def for_call(
        cls,
        name: str,
        args: dict,
        display: str | None = None,
        *,
        live: bool = True,
        timing: ToolTiming | None = None,
    ) -> CodeModePanel:
        """Build the same tool row for live events and saved calls."""
        if name in CODE_MODE_TOOLS:
            code = args.get("code", "")
            return cls.from_source(
                name, code if isinstance(code, str) else "", None, live=live, timing=timing
            )
        if display is None:
            from novacode_cli.ui.ui_elements import format_tool_display

            try:
                display = format_tool_display(name, args)
            except (AttributeError, TypeError, ValueError):
                # Invalid arguments are still useful in the recorded call.
                display = name
        return cls.from_source(
            name,
            json.dumps(args, indent=2, ensure_ascii=False),
            display,
            live=live,
            timing=timing,
        )

    @classmethod
    def from_source(
        cls,
        name: str,
        source: str,
        display: str | None,
        *,
        live: bool = True,
        timing: ToolTiming | None = None,
    ) -> CodeModePanel:
        """Build a tool row from its already formatted arguments or script."""
        from novacode_cli.tui.diff_preview import DiffPreview
        from novacode_cli.tui.widgets import OutputLog

        body = None
        if name in _SHELL_TOOLS:
            body = OutputLog(
                classes="terminal-log",
                highlight=True,
                markup=False,
                max_lines=2_000,
            )
        elif name in _DIFF_TOOLS:
            body = DiffPreview(classes="toolbody")
        panel = cls(
            source,
            tool_name=name,
            display_str=display,
            output_widget=body,
            streaming=live and isinstance(body, OutputLog),
            # A replayed call has no duration worth reporting; only live rows time.
            timing=timing if timing is not None else ToolTiming(enabled=live),
        )
        panel.add_class("tool")
        return panel

    def compose(self) -> ComposeResult:
        """Keep the summary visible while the disclosure hides only full details."""
        yield Horizontal(self.status, self.heading, self.timing_label, classes="code-mode-heading")
        yield self.preview
        yield self.note
        yield from super().compose()

    @property
    def unmounted_detail_chars(self) -> int:
        """Count retained text even while its disclosure widgets are absent."""
        return (
            len(self._source_text) if self.source is None or not self.source.is_mounted else 0
        ) + (len(self._output_text) if self.output is None or not self.output.is_mounted else 0)

    def attach_diff_block(self, block: Text) -> None:
        """Attach a diff for a write this call performed, in this call's details.

        A Code Mode snippet's edits have no row of their own; the eval row is the
        only place their diffs can appear. Held until the details mount (the row
        starts collapsed) and re-applied after ``finish`` replaces the output.
        """
        self._diff_blocks.append(block)
        from novacode_cli.tui.widgets import OutputLog  # local: widgets imports this module

        if isinstance(self.output, OutputLog):
            self.output.write(block)
        elif isinstance(self.output, Static):
            self.output.update(self._output_with_blocks())

    @property
    def diff_block_count(self) -> int:
        """How many diffs this call has attached (the renderer caps them)."""
        return len(self._diff_blocks)

    def _output_with_blocks(self) -> Text:
        """This call's output followed by the diffs of the writes inside it."""
        text = Text(self._output_text)
        for block in self._diff_blocks:
            text.append("\n")
            text.append_text(block)
        return text

    async def on_mount(self) -> None:
        self._refresh_timing()
        if self.has_class("tool-active") and self.timing.enabled:
            # A long call shows its elapsed time growing instead of staying blank.
            self._ticker = self.set_interval(1.0, self._refresh_timing)
        if not self.collapsed:
            await self._mount_details()

    def _stop_ticker(self) -> None:
        if self._ticker is not None:
            with suppress(Exception):  # noqa: BLE001 — the row outlives its timer
                self._ticker.stop()
            self._ticker = None

    def _refresh_timing(self) -> None:
        """Show this call's duration and the turn's elapsed time, when known."""
        from novacode_cli.tui.app import _paint

        text = self.timing.summary()
        with suppress(Exception):  # noqa: BLE001 — timing must never break a tool row
            # _paint: this fires per running tool per second; Static.update
            # defaults to layout=True, which re-arranges the whole screen.
            _paint(self.timing_label, text)
            self.timing_label.display = bool(text)

    async def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible is self and not self.collapsed:
            await self._mount_details()

    async def _mount_details(self) -> None:
        if self.source is not None:
            return
        contents = self.query_one(self.Contents)
        self.source = Static(Text(self._source_text), classes="code-mode-source")
        drawer = Collapsible(self.source, title=self._source_title, collapsed=True)
        if self.output is None:
            self.output = Static(
                self._output_with_blocks(), classes="code-mode-output", markup=False
            )
        if self.output.parent is contents:
            await contents.mount(drawer, before=self.output)
        else:
            from novacode_cli.tui.widgets import OutputLog

            if isinstance(self.output, OutputLog) and not self.has_class("tool-active"):
                self.output.write(Text(self._output_text))
            await contents.mount(drawer, self.output)

    def restore_result(self, output: str, *, is_error: bool, preview: str | None = None) -> None:
        """Paint a completed record without registering it as a live tool."""
        self.finish(output, is_error=is_error, preview=preview or _line(output, 110))

    def finish(self, output: str, *, is_error: bool, preview: str) -> None:
        """Update this run alone and surface failures without opening raw output."""
        self.timing.stop()
        try:
            data = json.loads(output) if self.display_str is None else None
        except (ValueError, TypeError):
            data = None
        title, note, failed = (
            _summarize(data, is_error=is_error, preview=preview, fallback=self.tool_name)
            if self.display_str is None
            else (self.display_str, preview if is_error and preview not in output else "", is_error)
        )
        # Rich Text keeps tool-provided brackets literal in the summary and result.
        self.heading.update(Text.assemble(("Failed " if failed else "Ran ", "bold"), title))
        self._output_text = output or preview or "(no output)"
        self.result_preview = preview
        selected = (
            data.get("error") or data.get("result", preview)
            if isinstance(data, dict)
            else output or preview
        )
        text = (
            selected
            if isinstance(selected, str)
            else json.dumps(selected, ensure_ascii=False, indent=2)
        )
        lines = text.splitlines() or ["(no output)"]
        shown = [
            ("└ " if index == 0 else "  ")
            + line[:_PREVIEW_WIDTH]
            + ("…" if len(line) > _PREVIEW_WIDTH else "")
            for index, line in enumerate(lines[:_PREVIEW_LINES])
        ]
        self.preview.update(Text("\n".join(shown)))
        self.preview.display = True
        omitted = max(0, len(lines) - _PREVIEW_LINES)
        more = f"+ {omitted} lines · " if omitted else ""
        self.title = f"{more}Full output (ctrl+o to expand)"
        if isinstance(selected, str) and note.splitlines()[:1] == [_line(selected)]:
            note = "\n".join(note.splitlines()[1:])
        self.note.update(note)
        self.note.display = bool(note) and (
            failed
            or (isinstance(data, dict) and bool(data.get("truncated") or data.get("state_warning")))
            or _cancelled_count(data) > 0
        )
        if isinstance(self.output, Static):
            self.output.update(self._output_with_blocks())
        self.set_class(failed, "failed")
        self.remove_class("tool-active")
        self._stop_ticker()
        self._refresh_timing()
