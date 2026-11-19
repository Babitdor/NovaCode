"""Paged, read-only scrollback for one conversation pane."""

from __future__ import annotations

import asyncio
import json
import logging
from array import array
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import messages_from_dict
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from novacode_cli.session.adapters import HarnessMessage
from novacode_cli.session.adapters.harness_native import normalize_messages
from novacode_cli.tui.code_mode_panel import CodeModePanel
from novacode_cli.tui.diff_preview import DiffPreview
from novacode_cli.tui.session_transcript import history_card
from novacode_cli.tui.widgets import ChatMessage, OutputLog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from langchain_core.messages import BaseMessage
    from textual.widget import Widget

    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.tui.widgets import TranscriptScroll

logger = logging.getLogger(__name__)
PAGE_ROWS = 20
_VISIBLE_PAGES = 2


def conversation_rows(messages: list[BaseMessage]) -> list[HarnessMessage]:
    """Restore only user-visible messages and tool calls from a Nova checkpoint."""
    from novacode_cli.compaction import is_compaction_summary
    from novacode_cli.core.streaming import is_internal_context_text
    from novacode_cli.session.imported_context import MESSAGE_ID

    visible = [
        message
        for message in messages
        if message.type in {"human", "ai", "tool"}
        and message.id != MESSAGE_ID
        and message.additional_kwargs.get("lc_source") not in ("summarization", "pinned_skill")
    ]
    return [
        row
        for row in normalize_messages(visible, pair_tools=True)
        if row.role == "tool"
        or not (is_compaction_summary(row.content) or is_internal_context_text(row.content))
    ]


def saved_rows(
    manager: SessionManager | None,
    session_id: str | None,
    source: list[BaseMessage],
    visible: list[BaseMessage],
    *,
    thread_id: str | None,
) -> list[HarnessMessage]:
    """Resolve saved messages and their pre-compaction originals, oldest first."""
    from novacode_cli.context.archives import default_recovery_store

    previous = []
    if session_id and callable(getattr(manager, "load_history", None)):
        previous = manager.load_history(session_id)
    store = default_recovery_store()
    manifests = (store.root / "manifests").resolve()
    visible_ids = {m.id for m in visible if m.id}
    visible_objects = {id(m) for m in visible}
    seen_ids: set[str] = set()
    inspected: set[Path] = set()
    original: list[BaseMessage] = []

    def append(messages: Iterable[BaseMessage]) -> None:
        for message in messages:
            attrs = message.additional_kwargs
            for reference in attrs.get("history_files") or ():
                if not isinstance(reference, str):
                    continue
                try:
                    path = Path(reference).resolve()
                    if path.parent != manifests or path.suffix != ".json" or path in inspected:
                        continue
                    inspected.add(path)
                    manifest, payload = store.inspect(path.stem)
                    if thread_id and manifest.get("thread") == thread_id:
                        append(messages_from_dict(payload["messages"]))
                except (OSError, ValueError, KeyError, TypeError):
                    logger.debug("Could not load a scrollback archive", exc_info=True)
            if id(message) in visible_objects or (
                message.id and (message.id in visible_ids or message.id in seen_ids)
            ):
                continue
            if message.id:
                seen_ids.add(message.id)
            if attrs.get("lc_source") == "summarization":
                continue
            original.append(message)

    append(previous)
    append(source)
    return conversation_rows(original)


def archived_row(widget: Widget) -> HarnessMessage | None:
    """Take text from a retired widget before Textual releases its contents."""
    if isinstance(widget, ChatMessage):
        if widget.raw_text:
            role: Literal["user", "assistant", "tool", "system"] = (
                "user" if widget.has_class("user") else "assistant"
            )
            if widget.has_class("reason"):
                role = "system"
            return HarnessMessage(role, widget.raw_text, metadata={"label": widget._header.plain})
        return None
    if isinstance(widget, CodeModePanel):
        return HarnessMessage(
            "tool",
            "No recorded result." if widget.has_class("tool-active") else widget._output_text,
            tool_name=widget.tool_name,
            metadata={
                "source": widget._source_text,
                "display": widget.display_str,
                "preview": widget.result_preview,
                "is_error": widget.has_class("failed"),
                "interrupted": widget.has_class("tool-active"),
                "collapsed": widget.collapsed,
                "diff": widget.output.archive() if isinstance(widget.output, DiffPreview) else None,
            },
        )

    parts = []
    for child in widget.walk_children(with_self=True):
        if isinstance(child, OutputLog):
            parts.extend(line.plain for line in child._output_lines)
        elif isinstance(child, Static):
            content = child.content
            if isinstance(content, Markdown):
                parts.append(content.markup)
            elif isinstance(content, Text):
                parts.append(content.plain)
            elif isinstance(content, str):
                parts.append(content)
    text = "\n".join(part for part in parts if part)
    return HarnessMessage("system", text) if text else None


class _ArchivedRows:
    def __init__(self, source: Callable[[], list[HarnessMessage]] | None = None) -> None:
        self.file = SpooledTemporaryFile(  # noqa: SIM115 -- Closed by the owning Scrollback.
            max_size=256 * 1024, mode="w+b"
        )
        self.offsets = array("Q")
        if source:
            for row in source():
                self.append(row)

    def __len__(self) -> int:
        return len(self.offsets)

    def append(self, row: HarnessMessage) -> None:
        payload = json.dumps(asdict(row), ensure_ascii=True).encode() + b"\n"
        offset = self.file.seek(0, 2)
        self.file.write(payload)
        self.offsets.append(offset)

    def read(self, start: int, end: int) -> list[HarnessMessage]:
        rows = []
        for index in range(start, end):
            self.file.seek(self.offsets[index])
            rows.append(HarnessMessage(**json.loads(self.file.readline())))
        return rows

    def close(self) -> None:
        self.file.close()
        del self.offsets[:]


class Scrollback(Vertical):
    """Keep the visible history and a small page buffer above the live transcript."""

    DEFAULT_CSS = """
    Scrollback { height: auto; }
    Scrollback > Vertical { height: auto; }
    Scrollback > .history-gap { height: 100vh; }
    """

    def __init__(self, scroll: TranscriptScroll) -> None:
        """Initialize storage owned by this scroll widget."""
        super().__init__()
        self.scroll = scroll
        self._live = _ArchivedRows()
        self._saved = _ArchivedRows()
        self._source: Callable[[], list[HarnessMessage]] | None = None
        self._pages: list[tuple[int, int, Vertical]] = []
        self._loading = False
        self._store_closed = False
        self._gap = Static(classes="history-gap")
        self._gap.display = False

    def compose(self):
        """Keep the live tail outside the viewport while newer pages are missing."""
        yield self._gap

    def _update_gap(self) -> None:
        self._gap.display = bool(
            self._pages and self._pages[-1][1] < len(self._saved) + len(self._live)
        )

    def scroll_changed(self, old_y: float, new_y: float) -> None:
        """Page in the direction of travel when the viewport reaches a seam."""
        if new_y < old_y and new_y <= 1:
            self.request()
        elif new_y > old_y and self._gap.display:
            seam = self._transcript_y(self._gap)
            # Explicit jumps can land directly in the live tail.
            if (
                seam <= new_y + self.scroll.scrollable_content_region.height
                and new_y < seam + self._gap.virtual_region.height
            ):
                self.request(older=False)

    def set_source(self, source: Callable[[], list[HarnessMessage]]) -> None:
        """Supply archived disk history to read only on the first older request."""
        self._source = source

    def append(self, row: HarnessMessage) -> None:
        """Append a retired live row without keeping its content in memory."""
        self._live.append(row)
        self._update_gap()

    def close(self) -> None:
        """Release the backing store on session reset or tab close."""
        self._store_closed = True
        self._live.close()
        self._saved.close()
        self._source = None

    def on_unmount(self) -> None:
        """Release this pane's temporary file."""
        self.close()

    def _read_page(self, start: int, end: int) -> list[HarnessMessage]:
        saved_end = len(self._saved)
        return [
            *self._saved.read(start, min(end, saved_end)),
            *self._live.read(max(start - saved_end, 0), end - saved_end),
        ]

    def request(self, *, older: bool = True) -> bool:
        """Open a page if the reader is in this pane and history is available."""
        if self._store_closed or self._loading or not self.is_mounted or not self.scroll.display:
            return False
        if older:
            if not self._source and (
                (self._pages and self._pages[0][0] == 0) or not (self._saved or self._live)
            ):
                return False
        elif not self._pages or self._pages[-1][1] >= len(self._saved) + len(self._live):
            return False
        self._loading = True
        self.run_worker(self._load_page(older=older), group="scrollback")
        return True

    async def _load_page(self, *, older: bool) -> None:
        try:
            if self._source:
                saved = await asyncio.to_thread(_ArchivedRows, self._source)
                if self._store_closed:
                    saved.close()
                    return
                self._source = None
                self._saved.close()
                self._saved = saved
            if not self.scroll.display:
                return
            total = len(self._saved) + len(self._live)
            if older:
                end = self._pages[0][0] if self._pages else total
                start = max(0, end - PAGE_ROWS)
            else:
                start = self._pages[-1][1]
                end = min(start + PAGE_ROWS, total)
            if start == end:
                return

            await self._show_page(start, end, older=older)
        except (OSError, ValueError):
            logger.warning("Could not load older conversation messages", exc_info=True)
            with suppress(Exception):
                self.notify("Could not load older messages.", severity="warning")
        finally:
            self._loading = False

    async def _show_page(self, start: int, end: int, *, older: bool) -> None:
        # Preserve the first row below the insertion, even if the user was
        # reading inside it. Anchor coordinates include the container offset.
        anchor = (
            self._pages[0][2]
            if self._pages
            else next((child for child in self.scroll.children if child is not self), None)
        )
        if not older:
            anchor = self._pages[-1][2]
        old_top = self._transcript_y(anchor) if anchor is not None else 0
        position = self.scroll.scroll_y
        self.scroll.pause_following()
        page = Vertical(*(
            history_card(row, native_scrollback=True) for row in self._read_page(start, end)
        ))
        if older and self._pages:
            await self.mount(page, before=self._pages[0][2])
        else:
            await self.mount(page, before=self._gap)
        if self._store_closed:
            return
        if older:
            self._pages.insert(0, (start, end, page))
        else:
            self._pages.append((start, end, page))
        self._update_gap()
        self.scroll.refresh(layout=True)
        await self.wait_for_refresh()
        if self._store_closed or not self.scroll.display:
            return

        offset = self._transcript_y(anchor) + position - old_top if anchor is not None else 0
        if not older:
            # A page-down or scrollbar drag may have landed inside the gap.
            offset = min(offset, self._transcript_y(page))
        # Retire only pages outside the viewport we are about to restore.
        # Keeping a partial page prevents a line jump when reversing direction.
        retire = []
        viewport = self.scroll.scrollable_content_region.height
        while len(self._pages) > _VISIBLE_PAGES:
            edge = self._pages[-1 if older else 0][2]
            top = self._transcript_y(edge)
            if older and top < offset + viewport:
                break
            if not older and top + edge.region.height > offset:
                break
            retire.append(self._pages.pop(-1 if older else 0)[2])
            if not older:
                offset -= edge.region.height
        if retire:
            await self.remove_children(retire)
            self._update_gap()
            self.scroll.refresh(layout=True)
            await self.wait_for_refresh()
        if not self._store_closed and self.scroll.display:
            self.scroll.scroll_to(y=offset, animate=False, immediate=True, force=True)

    def _transcript_y(self, widget: Widget) -> int:
        top = widget.virtual_region.y
        return top + self.virtual_region.y if widget.parent is self else top
