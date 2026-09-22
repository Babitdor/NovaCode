"""Remote status line: one edit-in-place message showing what the agent is doing.

The answer to a remote turn is sent as its own chat message (``reply_fn``). This
module owns a *separate* message, edited in place while the turn runs, that
shows the work as a compact markdown log:

    ⚙️ **Working** · 3 tools · 0:12
    ▶️ Add endpoint                         (the plan, when there is one)
    > 💭 _…the tail of the model's reasoning_
    ✓ `read_file(process.py)`
    ⏳ `shell("pytest -q")`

and settles, when the turn ends, to a summary with the reasoning and the full
tool log folded into quotes (Telegram shows long quotes collapsed):

    ✅ **Done** · 5 tools · 0:23
    > **💭 Reasoning**
    > …
    > **🔧 Tools**
    > ✓ `read_file(process.py)` …

Edits are coalesced (~1.3 s) so a burst of tool calls is one edit. It needs only
an async ``edit_fn(markdown, final=False)``, which both bridges provide.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

EditFn = Callable[..., Awaitable[None]]  # edit_fn(text, final=False)

_PUMP_INTERVAL = 1.3
_WORKING = "⚙️ **Working…**"
_TODO_GLYPH: dict[str, str] = {"completed": "✅", "in_progress": "▶️", "pending": "☐"}
_TODO_MAX = 8
#: Tool lines shown live (newest last); the settled log shows all, up to _LOG_MAX.
_TOOLS_LIVE = 6
_LOG_MAX = 40
#: Live tails: the part a watching user is reading, not a transcript.
_REASONING_LIVE = 400
_PROSE_LIVE = 300
#: Reasoning kept in the settled message. The whole message must fit one
#: Telegram message (4096) / stay readable on Discord.
_REASONING_FINAL = 2000
_DETAIL_MAX = 70


@dataclass
class _Call:
    name: str
    detail: str
    call_id: str | None
    state: str = "running"  # running | ok | error


def _quote(text: str) -> str:
    """Markdown blockquote of ``text`` (every line prefixed)."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def _clip_tail(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else "…" + text[-limit:].lstrip()


def _code(text: str) -> str:
    """Inline code that survives backticks inside ``text``."""
    return f"`{text.replace('`', 'ʼ')}`"


class RemoteStatusLine:
    """One edit-in-place status message: plan, reasoning, and a live tool log."""

    def __init__(
        self, edit_fn: EditFn, *, interval: float = _PUMP_INTERVAL, label: str = ""
    ) -> None:
        self._edit = edit_fn
        self._label = label  # the session's name when several are open
        self._calls: list[_Call] = []
        self._todos: list[tuple[str, str]] = []  # (content, status)
        self._reasoning = ""
        self._prose = ""
        self._dirty = False
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._closed = False
        self._started = time.monotonic()

    # ── inputs ─────────────────────────────────────────────────────────────

    def note(self, name: str, detail: str | None = None, call_id: str | None = None) -> None:
        """Record a tool call (``detail``: its one-line display, e.g. ``read_file(a.py)``)."""
        if not name:
            return
        shown = str(detail or name)
        if name == "task" and not detail:
            shown = "🤖 subagent"
        if len(shown) > _DETAIL_MAX:
            shown = shown[: _DETAIL_MAX - 1] + "…"
        known = next((c for c in self._calls if call_id and c.call_id == call_id), None)
        if known is not None:  # the same call reported twice (tool call + subagent)
            known.detail = shown
            self._dirty = True
            return
        self._calls.append(_Call(str(name), shown, call_id))
        self._prose = ""  # prose before a tool call was narration, not the answer
        self._dirty = True

    def note_result(self, call_id: str | None, *, error: bool = False) -> None:
        """Mark a tool call finished (matched by id, else the oldest running one)."""
        match = next((c for c in self._calls if call_id and c.call_id == call_id), None)
        if match is None:
            match = next((c for c in self._calls if c.state == "running"), None)
        if match is not None:
            match.state = "error" if error else "ok"
            self._dirty = True

    def note_text(self, text: str, *, kind: str = "text") -> None:
        """Record streamed model output: ``kind="reasoning"`` or answer prose."""
        if not text:
            return
        if kind == "reasoning":
            self._reasoning += text
        else:
            self._prose += text
        self._dirty = True

    def reset_text(self) -> None:
        """Drop streamed prose the terminal suppressed (internal scratchpad)."""
        if self._prose:
            self._prose = ""
            self._dirty = True

    def note_todos(self, todos: list) -> None:
        """Set the current plan (``write_todos``). Shown live, in place."""
        parsed: list[tuple[str, str]] = []
        for td in todos or []:
            if isinstance(td, dict):
                content = str(td.get("content", "")).strip()
                if content:
                    parsed.append((content, td.get("status", "pending")))
        self._todos = parsed
        self._dirty = True

    # ── rendering ──────────────────────────────────────────────────────────

    def _elapsed(self) -> str:
        secs = int(time.monotonic() - self._started)
        return f"{secs // 60}:{secs % 60:02d}"

    def _header(self, done: bool) -> str:
        n = len(self._calls)
        parts = ["✅ **Done**" if done else "⚙️ **Working**"]
        if self._label:
            parts.insert(0, f"`{self._label}`")
        if n:
            parts.append(f"{n} tool{'s' if n != 1 else ''}")
        parts.append(self._elapsed())
        return " · ".join(parts)

    def _todo_lines(self) -> list[str]:
        shown = self._todos[:_TODO_MAX]
        lines = [f"{_TODO_GLYPH.get(status, '☐')} {content}" for content, status in shown]
        if len(self._todos) > len(shown):
            lines.append(f"… +{len(self._todos) - len(shown)} more")
        return lines

    @staticmethod
    def _call_line(call: _Call) -> str:
        mark = {"running": "⏳", "ok": "✓", "error": "✗"}[call.state]
        return f"{mark} {_code(call.detail)}"

    def _content(self) -> str:
        """The live message."""
        lines = [self._header(done=False)]
        lines += self._todo_lines()
        if self._reasoning.strip():
            lines.append(_quote(f"💭 _{_clip_tail(self._reasoning, _REASONING_LIVE)}_"))
        if self._calls:
            hidden = len(self._calls) - _TOOLS_LIVE
            if hidden > 0:
                lines.append(f"… {hidden} earlier")
            lines += [self._call_line(c) for c in self._calls[-_TOOLS_LIVE:]]
        if self._prose.strip():
            lines.append(f"✍️ {_clip_tail(self._prose, _PROSE_LIVE)}")
        # Blank lines keep the quote from swallowing the lines after it.
        return "\n\n".join(block for block in _blocks(lines))

    def _done_summary(self) -> str:
        """The settled message: header, plan, then reasoning and tools folded."""
        sections = ["\n".join([self._header(done=True), *self._todo_lines()])]
        if self._reasoning.strip():
            reasoning = self._reasoning.strip()
            if len(reasoning) > _REASONING_FINAL:
                reasoning = "…" + reasoning[-_REASONING_FINAL:]
            sections.append(_quote(f"**💭 Reasoning**\n{reasoning}"))
        if self._calls:
            log = [self._call_line(c) for c in self._calls[:_LOG_MAX]]
            if len(self._calls) > _LOG_MAX:
                log.append(f"… +{len(self._calls) - _LOG_MAX} more")
            sections.append(_quote("**🔧 Tools**\n" + "\n".join(log)))
        return "\n\n".join(sections)

    # ── pump ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the coalescing pump (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        last: str | None = None
        try:
            first = self._content()
            await self._edit(first, False)  # immediate first paint
            last = first
        except Exception:  # noqa: BLE001
            logger.debug("status first paint failed", exc_info=True)
        self._dirty = False
        try:
            while not self._closed:
                await asyncio.sleep(self._interval)
                if not self._dirty:
                    continue
                self._dirty = False
                content = self._content()
                if content != last:
                    last = content
                    try:
                        await self._edit(content, False)
                    except Exception:  # noqa: BLE001 — a dropped edit is non-fatal
                        logger.debug("status edit failed", exc_info=True)
        except asyncio.CancelledError:
            return

    async def finalize(self) -> None:
        """Stop the pump and settle to the summary.

        Answer prose is dropped: the answer is sent as its own message right
        after this, and keeping it here too showed it twice.
        """
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        for call in self._calls:  # a result we never heard about still finished
            if call.state == "running":
                call.state = "ok"
        try:
            await self._edit(self._done_summary(), True)
        except Exception:  # noqa: BLE001
            logger.debug("status finalize failed", exc_info=True)


def _blocks(lines: list[str]) -> list[str]:
    """Group consecutive plain lines; a quote always stands alone."""
    out: list[str] = []
    run: list[str] = []
    for line in lines:
        if line.startswith(">"):
            if run:
                out.append("\n".join(run))
                run = []
            out.append(line)
        else:
            run.append(line)
    if run:
        out.append("\n".join(run))
    return out


def feed(status: RemoteStatusLine, event: object) -> None:
    """Route one UI stream event into a status line.

    Called per session from ``NovaApp._deliver`` — not from rendering, which
    only runs for the pane on screen: a remote turn in a background tab (or the
    main session while you look at another tab) would otherwise freeze.
    """
    from novacode_cli import ui_events as ev

    if isinstance(event, ev.ReasoningDelta):
        status.note_text(event.text, kind="reasoning")
    elif isinstance(event, ev.TextDelta):
        status.note_text(event.text, kind="text")
    elif isinstance(event, ev.TextDiscard):
        status.reset_text()
    elif isinstance(event, ev.ToolCall):
        status.note(event.name, event.display_str, event.call_id)
    elif isinstance(event, ev.ToolResult):
        status.note_result(event.call_id, error=event.is_error)
    elif isinstance(event, ev.FileOp):
        rec = event.record
        errored = bool(getattr(rec, "error", None)) or getattr(rec, "status", "") == "error"
        status.note_result(event.call_id, error=errored)
    elif isinstance(event, ev.TodoUpdate):
        status.note_todos(event.todos)
    elif isinstance(event, ev.SubagentActivity):
        if event.kind == "dispatched":
            status.note("task", f"🤖 {event.subagent_type or 'subagent'}", event.call_id)
        elif event.kind == "completed":
            status.note_result(event.call_id)
