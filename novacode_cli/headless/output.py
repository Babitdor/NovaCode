"""Output formatting for headless (non-interactive) Nova runs.

Headless mode is another renderer of the UI-agnostic agent event stream (see
:mod:`novacode_cli.ui_events`), alongside the Textual TUI (and the console
renderer ``execute_task``, which the remote bridges and several command
handlers use). Where those render to a live terminal, this writes
machine-consumable output to **stdout** in one of three formats:

- ``text``        — only the final assistant answer (good for piping).
- ``json``        — a single result object emitted at the end.
- ``stream-json`` — newline-delimited JSON (NDJSON): an ``init`` line, one line
  per agent event, then a final ``result`` line.

The ``stream-json`` payloads are *Nova-event-shaped* (documented below), not
byte-identical to the Anthropic API message format — Nova's events are not raw
API messages.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from novacode_cli import ui_events as ev

VALID_FORMATS = ("text", "json", "stream-json")


class HeadlessOutput:
    """Render headless agent events to a stream in the chosen format."""

    def __init__(
        self,
        fmt: str,
        session_id: str,
        model_name: str | None,
        stream: TextIO | None = None,
        fd: int | None = None,
    ) -> None:
        """Build a formatter for ``fmt``.

        Output goes to a raw file descriptor ``fd`` (via ``os.write``) when
        given — this is how the runner targets a dup of fd 1 that survives a
        stdio MCP server closing the Python-level ``sys.stdout`` object. When no
        ``fd`` is given (e.g. tests), it falls back to ``stream`` / stdout.
        """
        if fmt not in VALID_FORMATS:
            msg = f"Unknown output format: {fmt!r} (expected one of {VALID_FORMATS})"
            raise ValueError(msg)
        self.fmt = fmt
        self.session_id = session_id
        self.model_name = model_name
        self._fd = fd
        # Resolve the stream *now* (not at class-def time) so tests can swap it.
        self._stream = stream if stream is not None else sys.stdout

    # -- low-level helpers ------------------------------------------------
    def _write(self, text: str) -> None:
        """Write raw text to the fd (preferred) or the stream."""
        if self._fd is not None:
            os.write(self._fd, text.encode("utf-8", errors="replace"))
            return
        self._stream.write(text)
        self._stream.flush()

    def _writeln(self, obj: Any) -> None:
        """Write one NDJSON line."""
        self._write(json.dumps(obj, ensure_ascii=False) + "\n")

    # -- lifecycle --------------------------------------------------------
    def init(self) -> None:
        """Emit the stream-json init line (no-op for text/json)."""
        if self.fmt == "stream-json":
            self._writeln(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "model": self.model_name,
                }
            )

    def handle_event(self, event: Any) -> None:
        """Render a single agent event (stream-json only; others accumulate).

        For ``text`` and ``json`` modes nothing is emitted per-event — the
        final answer is assembled by the runner and emitted via :meth:`result`.
        """
        if self.fmt != "stream-json":
            return
        line = self._map_event(event)
        if line is not None:
            self._writeln(line)

    def result(
        self,
        *,
        subtype: str,
        is_error: bool,
        result_text: str,
        num_turns: int,
        duration_ms: int,
        usage: dict[str, int],
    ) -> None:
        """Emit the terminal result in the active format."""
        if self.fmt == "text":
            # Only the final answer goes to stdout. Errors still print their
            # message so a piped consumer sees *something*.
            text = result_text or ""
            if not text and is_error:
                text = f"[{subtype}]"
            if text:
                self._write(text.rstrip("\n") + "\n")
            return

        payload = {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": result_text,
            "session_id": self.session_id,
            "num_turns": num_turns,
            "duration_ms": duration_ms,
            "usage": usage,
        }
        self._writeln(payload)

    # -- event mapping ----------------------------------------------------
    @staticmethod
    def _map_event(event: Any) -> dict[str, Any] | None:  # noqa: PLR0911 — event dispatch
        """Map a ui_event to an NDJSON dict, or None to skip it.

        Streaming deltas and spinner/status noise are intentionally dropped;
        only committed, meaningful events are surfaced.
        """
        if isinstance(event, ev.AssistantMessage):
            return {
                "type": "assistant",
                "subtype": "text",
                "text": event.text,
                "agent": event.agent_name,
                "is_subagent": event.is_subagent,
            }
        if isinstance(event, ev.ToolCall):
            return {
                "type": "tool_use",
                "name": event.name,
                "input": event.args,
                "id": event.call_id,
                "display": event.display_str,
            }
        if isinstance(event, ev.ToolResult):
            return {
                "type": "tool_result",
                "tool_use_id": event.call_id,
                "is_error": event.is_error,
                "content": event.full_output or event.preview,
            }
        if isinstance(event, ev.FileOp):
            record = event.record
            return {
                "type": "file_op",
                "operation": getattr(record, "operation", None) or getattr(record, "op", None),
                "path": str(getattr(record, "path", "") or getattr(record, "file_path", "")),
            }
        if isinstance(event, ev.TodoUpdate):
            return {"type": "todo", "todos": event.todos}
        if isinstance(event, ev.ErrorOutput):
            return {"type": "error_output", "text": event.text}
        if isinstance(event, ev.ContextMessage):
            return {
                "type": "system",
                "subtype": "context",
                "message": event.message,
                "event_type": event.event_type,
            }
        if isinstance(event, ev.Error):
            return {
                "type": "error",
                "message": event.message,
                "is_provider_notice": event.is_provider_notice,
            }
        # StatusUpdate / TextDelta / ReasoningDelta / TextDiscard / UsageUpdate /
        # SubagentActivity / CompactionNotice / Cancelled / Done — handled by the
        # runner or deliberately not surfaced as their own NDJSON line.
        return None
