"""Call-scoped source data, separate from tool-result presentation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from deepagents.backends.protocol import ReadResult
    from langchain_core.messages import ToolMessage

DISPLAY_NOTE = "nova_display_note"
MAX_INTERMEDIATE_CHARS = 1_000_000
_MAX_CAPTURED_READS = 2


def data_text(content: str, metadata: Mapping[str, object]) -> str:
    """Undo only our own decoration when subsequent middleware left it intact."""
    note = metadata.get(DISPLAY_NOTE)
    if (
        isinstance(note, dict)
        and isinstance(note.get("before"), str)
        and note.get("after") == content
    ):
        return note["before"]
    return content


def append_display_note(result: ToolMessage, note: str) -> ToolMessage:
    """Preserve machine data while accumulating human-visible diagnostics."""
    if not isinstance(result.content, str):
        return result
    rendered = result.content + note
    return result.model_copy(
        update={
            "content": rendered,
            "additional_kwargs": {
                **result.additional_kwargs,
                DISPLAY_NOTE: {
                    "before": data_text(result.content, result.additional_kwargs),
                    "after": rendered,
                },
            },
        }
    )


@dataclass
class ReadCapture:
    """Hold only the reads performed by one guarded native invocation."""

    pages: list[ReadResult] = field(default_factory=list)
    active: bool = True
    too_large: bool = False

    def record(self, page: ReadResult) -> None:
        """Retain one result and an ambiguity sentinel without unbounded buffering."""
        if not self.active:
            return
        if page.file_data and len(page.file_data["content"]) > MAX_INTERMEDIATE_CHARS:
            self.too_large = True
            return
        if len(self.pages) < _MAX_CAPTURED_READS:
            self.pages.append(page)

    def source_text(self, display: object) -> str | None:
        """Return exact text only when the complete rendered page survived middleware."""
        if self.too_large:
            message = "Read exceeds the intermediate size limit; request a smaller offset/limit."
            raise ValueError(message)
        if not self.pages:
            return None
        if len(self.pages) != 1:
            message = "Ambiguous filesystem read; request one smaller source page."
            raise ValueError(message)
        page = self.pages[0]
        if page.start_line is None or not page.file_data or page.file_data["encoding"] != "utf-8":
            return None
        from deepagents.backends.utils import format_content_with_line_numbers

        raw = page.file_data["content"]
        rendered = format_content_with_line_numbers(raw, start_line=page.start_line)
        if not isinstance(display, str) or rendered not in display:
            message = "Read output was transformed or truncated; request a smaller offset/limit."
            raise ValueError(message)
        return raw


current_read_capture: ContextVar[ReadCapture | None] = ContextVar("nova_read_capture", default=None)


@contextmanager
def capture_file_read(*, enabled: bool) -> Iterator[ReadCapture | None]:
    """Isolate concurrent calls and stop accepting late results after cancellation."""
    capture = ReadCapture() if enabled else None
    token = current_read_capture.set(capture)
    try:
        yield capture
    finally:
        if capture is not None:
            capture.active = False
        current_read_capture.reset(token)
