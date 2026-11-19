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
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from novacode_cli.session.adapters import HarnessMessage
from novacode_cli.session.adapters.harness_native import normalize_messages
from novacode_cli.tui.code_mode_panel import CodeModePanel
from novacode_cli.tui.session_transcript import history_card
from novacode_cli.tui.widgets import ChatMessage, OutputLog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from langchain_core.messages import BaseMessage
    from textual.app import ComposeResult
    from textual.widget import Widget

    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.tui.widgets import TranscriptScroll

logger = logging.getLogger(__name__)
PAGE_ROWS = 20
_VISIBLE_PAGES = 2


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
    return normalize_messages(original)


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
            "tool", f"{widget._source_text}\n\n{widget._output_text}", tool_name=widget.tool_name
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
    """Keep at most two historical pages mounted above the live transcript."""

    DEFAULT_CSS = """
    Scrollback { height: auto; }
    Scrollback > Horizontal { height: 3; }
    Scrollback > Vertical { height: auto; }
    Scrollback Button { width: auto; min-width: 9; }
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
        self._older = Button("Load older messages")
        self._newer = Button("Load newer messages")
        self._newer_row = Horizontal(self._newer)
        self._newer_row.display = False

    def compose(self) -> ComposeResult:
        """Surround mounted history pages with their navigation controls."""
        yield Horizontal(self._older)
        yield self._newer_row

    def set_source(self, source: Callable[[], list[HarnessMessage]]) -> None:
        """Supply archived disk history to read only on the first older request."""
        self._source = source

    def append(self, row: HarnessMessage) -> None:
        """Append a retired live row without keeping its content in memory."""
        self._live.append(row)
        if self._pages:
            self._newer_row.display = True
        self._older.disabled = bool(self._pages and self._pages[0][0] == 0)

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Navigate within this pane without invoking the agent."""
        if event.button in (self._older, self._newer):
            event.stop()
            self.request(older=event.button is self._older)

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
                self._older.disabled = True
                self.display = bool(self._pages)
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
        old_top = anchor.region.y if anchor is not None else 0
        position = self.scroll.scroll_y
        self.scroll.pause_following()
        page = Vertical(*(history_card(row) for row in self._read_page(start, end)))
        if older and self._pages:
            await self.mount(page, before=self._pages[0][2])
        else:
            await self.mount(page, before=self._newer_row)
        if self._store_closed:
            return
        if older:
            self._pages.insert(0, (start, end, page))
        else:
            self._pages.append((start, end, page))
        if len(self._pages) > _VISIBLE_PAGES:
            await self._pages.pop(-1 if older else 0)[2].remove()
        self._older.disabled = self._pages[0][0] == 0
        self._newer_row.display = self._pages[-1][1] < len(self._saved) + len(self._live)

        self.scroll.refresh(layout=True)
        await self.wait_for_refresh()
        if self._store_closed or not self.scroll.display:
            return
        if anchor is None or not anchor.is_attached:
            self.scroll.scroll_to(y=0, animate=False, immediate=True)
        else:
            target_top = old_top if older else self.scroll.region.y
            offset = position + anchor.region.y - target_top
            self.scroll.scroll_to(y=offset, animate=False, immediate=True, force=True)
