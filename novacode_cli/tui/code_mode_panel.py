"""Compact tool rows, with recorded operation summaries for Code Mode runs."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import Collapsible, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widget import Widget

CODE_MODE_TOOLS = frozenset({"eval", "MCPCode"})
_MAX_TOOL_NAMES = 3
_PREVIEW_LINES = 3
_PREVIEW_WIDTH = 160


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
            or any(
                item.get("status") == "failed" or item.get("status") == "cancelled"
                for item in outcomes
            )
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
    CodeModePanel > CollapsibleTitle {
        color: $text-muted; padding: 0 0 0 2; text-style: none;
    }
    CodeModePanel > Contents { padding-left: 2; }
    CodeModePanel .code-mode-note { height: auto; padding-left: 2; color: $text-muted; }
    CodeModePanel .code-mode-preview {
        height: auto; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    CodeModePanel .code-mode-source, CodeModePanel .code-mode-output { height: auto; }
    CodeModePanel Collapsible {
        border: none; background: transparent; padding: 0;
    }
    CodeModePanel.failed .code-mode-note { color: $error; }
    """

    def __init__(
        self,
        code: str,
        *,
        tool_name: str = "eval",
        display_str: str | None = None,
        output_widget: Widget | None = None,
    ) -> None:
        """Keep the exact script and result behind separate disclosure controls."""
        self.status = Static("•", classes="code-mode-status")
        self.heading = Static(Text(f"Running {tool_name}"), classes="code-mode-label")
        self.preview = Static("", classes="code-mode-preview", markup=False)
        self.preview.display = False
        self.note = Static("", classes="code-mode-note", markup=False)
        self.note.display = False
        self.tool_name = tool_name
        self.display_str = display_str
        self.source = Static(Text(code or "(script unavailable)"), classes="code-mode-source")
        self.output = (
            output_widget
            if output_widget is not None
            else Static("Waiting for output.", classes="code-mode-output", markup=False)
        )
        source_title = "JavaScript" if display_str is None else "Arguments"
        super().__init__(
            Collapsible(self.source, title=source_title, collapsed=True),
            self.output,
            title=f"{source_title} & full output (Enter to expand)",
            collapsed=True,
            collapsed_symbol="",
            expanded_symbol="",
            classes="tool-active",
        )
        if display_str is not None:
            self.heading.update(Text(f"Running {display_str}"))

    def compose(self) -> ComposeResult:
        """Keep the summary visible while the disclosure hides only full details."""
        yield Horizontal(self.status, self.heading, classes="code-mode-heading")
        yield self.preview
        yield self.note
        yield from super().compose()

    def finish(self, output: str, *, is_error: bool, preview: str) -> None:
        """Update this run alone and surface failures without opening raw output."""
        title, note, failed = (
            summarize_run(output, is_error=is_error, preview=preview, fallback=self.tool_name)
            if self.display_str is None
            else (self.display_str, preview if is_error and preview not in output else "", is_error)
        )
        # Rich Text keeps tool-provided brackets literal in the summary and result.
        self.heading.update(Text.assemble(("Failed " if failed else "Ran ", "bold"), title))
        try:
            data = json.loads(output) if self.display_str is None else None
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            data = None
            pretty = output or "(no output)"
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
        self.title = f"{more}Full output (Enter to expand)"
        if isinstance(selected, str) and note.splitlines()[:1] == [_line(selected)]:
            note = "\n".join(note.splitlines()[1:])
        self.note.update(note)
        self.note.display = bool(note) and (
            failed
            or (isinstance(data, dict) and bool(data.get("truncated") or data.get("state_warning")))
        )
        if isinstance(self.output, Static):
            self.output.update(Text(pretty if self.display_str is None else output or preview))
        self.set_class(failed, "failed")
        self.remove_class("tool-active")
