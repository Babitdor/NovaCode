"""Compact, inspectable Code Mode runs using the runtime's recorded outcomes."""

from __future__ import annotations

import json
from collections import Counter

from rich.text import Text
from textual.widgets import Collapsible, Static

CODE_MODE_TOOLS = frozenset({"eval", "MCPCode"})
_MAX_TOOL_NAMES = 3


def _line(value: str, limit: int = 120) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _operation_name(item: dict) -> str:
    name, server = item.get("tool_name"), item.get("server_name")
    if isinstance(name, str) and name:
        return f"{server}/{name}" if isinstance(server, str) and server else name
    namespace, action = item.get("namespace"), item.get("action")
    return f"{namespace}.{action}" if isinstance(namespace, str) and isinstance(action, str) else ""


def summarize_run(output: str, *, is_error: bool, preview: str) -> tuple[str, str, bool]:
    """Summarize reported operations, never infer executed calls from JavaScript."""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        data = None
    title = "Code Mode"
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
        calls = data.get("calls")
        if type(calls) is int and calls >= 0:
            title += f" · {calls} call" + ("" if calls == 1 else "s")
        if names:
            shown = [
                f"{_line(name, 40)} x{count}" if count > 1 else _line(name, 40)
                for name, count in list(names.items())[:_MAX_TOOL_NAMES]
            ]
            title += " · " + ", ".join(shown)
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
        if type(omitted) is int and omitted > 0:
            title += f" · {omitted} records omitted"
        notes = [note] if note else []
        notes.extend(
            _line(data[key], 400)
            for key in ("hint", "recovery", "notice", "state_warning")
            if isinstance(data.get(key), str) and data[key]
        )
        if data.get("truncated") is True:
            notes.append(
                "Selected output is truncated; inspect smaller slices before relying on it."
            )
        note = "\n".join(notes)
    if failed and not note:
        note = _line(preview or "The script failed. Expand Full output for details.")
    title += " · failed" if failed else " · complete"
    return title, note, failed


class CodeModePanel(Collapsible):
    """One quiet row per script; source and output stay independently inspectable."""

    DEFAULT_CSS = """
    CodeModePanel { height: auto; }
    CodeModePanel .code-mode-note { height: auto; margin-bottom: 1; }
    CodeModePanel .code-mode-source, CodeModePanel .code-mode-output { height: auto; }
    CodeModePanel.failed > CollapsibleTitle { color: $error; }
    CodeModePanel .code-mode-note { color: $text-muted; }
    CodeModePanel.failed .code-mode-note { color: $error; }
    """

    def __init__(self, code: str) -> None:
        """Keep the exact script and result behind separate disclosure controls."""
        self.note = Static("Script is running.", classes="code-mode-note", markup=False)
        self.source = Static(Text(code or "(script unavailable)"), classes="code-mode-source")
        self.output = Static("Waiting for output.", classes="code-mode-output", markup=False)
        super().__init__(
            self.note,
            Collapsible(self.source, title="JavaScript", collapsed=True),
            Collapsible(self.output, title="Full output", collapsed=True),
            title="Code Mode · running",
            collapsed=True,
        )

    def finish(self, output: str, *, is_error: bool, preview: str) -> None:
        """Update this run alone and surface failures without opening raw output."""
        title, note, failed = summarize_run(output, is_error=is_error, preview=preview)
        # Rich Text keeps tool-provided brackets literal in a disclosure title.
        self.title = Text(title)
        self.note.update(
            note or "The script completed. Expand Full output to inspect its selected result."
        )
        try:
            pretty = json.dumps(json.loads(output), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            pretty = output or "(no output)"
        self.output.update(Text(pretty))
        self.set_class(failed, "failed")
        self.remove_class("tool-active")
        if failed:
            self.collapsed = False
