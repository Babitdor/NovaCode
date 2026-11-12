"""Project-grouped saved and live conversations, with a read-only details pane."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import groupby
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from textual import events
    from textual.app import ComposeResult

    from novacode_cli.session.session_persistence import SessionManager, SessionMeta

logger = logging.getLogger(__name__)
STATUSES = ("All", "Needs you", "Working", "Ready", "Inactive")
_PAGE_SIZE = 25
_DETAIL_CHARS = 1600
_NARROW_WIDTH = 100
_TINY_WIDTH = 72


@dataclass(frozen=True)
class CommandSession:
    """Detached display facts; selecting one grants no execution permission."""

    key: str
    session_id: str = ""
    pane_id: str = ""
    current: bool = False
    title: str = ""
    project: str = ""
    status: str = "Inactive"
    updated_at: str = ""
    model: str = ""
    reasoning: str = ""
    branch: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    last_message: str = ""
    prompt: str = ""
    blocked_reason: str = ""
    external: bool = False


def _text(content: object) -> str:
    if isinstance(content, str):
        return content[:_DETAIL_CHARS]
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )[:_DETAIL_CHARS]
    return ""


def message_preview(messages: Sequence[Any]) -> tuple[str, str]:
    """Use only human/assistant text, excluding tools and hidden reasoning blocks."""
    prompt = reply = ""
    for message in reversed(messages):
        kind = getattr(message, "type", "")
        if kind == "human" and not prompt:
            prompt = _text(message.content)
        elif kind == "ai" and not reply:
            reply = _text(message.content)
        if prompt and reply:
            break
    return prompt, reply


def _safe_identity(identity: str) -> bool:
    return (
        isinstance(identity, str)
        and bool(identity)
        and identity not in {".", ".."}
        and not any(character in identity for character in ("/", "\\", ":", "\0"))
    )


def pane_activity(app: Any, pane: Any) -> str:  # noqa: ANN401 - cached UI state
    """Share one status interpretation between tabs and Command Center."""
    if pane.status in {"crashed", "exited"}:
        return pane.status
    active = pane is getattr(app, "_active_pane", None)
    values = vars(app) if active else pane.state
    session = values.get("session_state")
    pending = getattr(session, "pending_approval_count", lambda: 0)()
    question = getattr(app, "_question_future", None) if active else None
    if (
        pane.pending_interrupt is not None
        or pending
        or (question is not None and not question.done())
        or pane.status == "needs-approval"
    ):
        return "needs-approval"
    # Hidden child panes retain a render snapshot until selected. Their process
    # events, rather than that stale _turn_active value, own their activity.
    if pane.kind == "root" and values.get("_turn_active"):
        return "running"
    return pane.status


def live_sessions(app: Any) -> list[CommandSession]:  # noqa: ANN401 - UI snapshot adapter
    """Read cached pane state without filesystem, model or tool calls."""
    rows = []
    for pane in getattr(app, "_panes", ()):
        active = pane is getattr(app, "_active_pane", None)
        values = vars(app) if active else pane.state
        value = values.get

        session = value("session_state")
        session_id = str(getattr(session, "session_id", "") or "")
        pane_status = pane_activity(app, pane)
        if pane_status in {"crashed", "exited"}:
            status = "Inactive"
        elif pane_status == "needs-approval":
            status = "Needs you"
        elif pane_status in {"running", "starting"}:
            status = "Working"
        else:
            status = "Ready"
        prompt, reply = message_preview(value("_restored_messages", ()) or ())
        owns_prompt = (
            pane.kind == "root"
            and getattr(session, "thread_id", None) is not None
            and getattr(app, "_last_user_prompt_thread", None) == session.thread_id
        )
        prompt = (
            (getattr(app, "_last_user_prompt", "") if owns_prompt else "")
            or pane.remote_prompt
            or prompt
        )
        reply = value("_accumulated_reply", "") or reply
        tracker = value("token_tracker")
        measured = bool(tracker and getattr(tracker, "has_api_data", False))
        incoming = int(getattr(tracker, "session_input_tokens", 0)) if measured else None
        outgoing = (
            max(0, int(getattr(tracker, "session_total_tokens", 0)) - incoming)
            if incoming is not None
            else None
        )
        rows.append(
            CommandSession(
                key=f"session:{session_id}" if session_id else f"pane:{pane.sid}",
                session_id=session_id,
                pane_id=pane.sid,
                current=active,
                title=(str(prompt).splitlines()[0] if prompt else "")
                or (pane.title if pane.kind != "root" else ""),
                project=str(
                    pane.project_root
                    or pane.worktree
                    or getattr(session, "workspace_root", "")
                    or ""
                ),
                status=status,
                model=str(value("model_name", "") or ""),
                reasoning=str(value("_reasoning_effort", "") or ""),
                branch=str(
                    pane.branch
                    or (
                        getattr(app, "_status_branch", "") if active and pane.kind == "root" else ""
                    )
                ),
                input_tokens=incoming,
                output_tokens=outgoing,
                last_message=_text(reply),
                prompt=_text(prompt),
            )
        )
    return rows


def merge_sessions(
    saved: Sequence[SessionMeta], live: Sequence[CommandSession]
) -> list[CommandSession]:
    """Overlay live facts; an old saved 'active' flag is never proof of running work."""
    rows = {
        f"session:{meta.session_id}": CommandSession(
            key=f"session:{meta.session_id}",
            session_id=meta.session_id,
            title=meta.current_task or "Saved conversation",
            project=meta.project_root or "",
            status="Needs you" if meta.task_status == "blocked" else "Inactive",
            updated_at=meta.last_active,
            model=meta.model_name or "",
            blocked_reason=meta.blocked_reason or "",
        )
        for meta in saved
        if not meta.cleared and _safe_identity(meta.session_id)
    }
    for live_row in live:
        row = live_row
        previous = rows.get(row.key)
        if previous is not None:
            row = replace(
                row,
                title=row.title or previous.title,
                project=row.project or previous.project,
                model=row.model or previous.model,
                updated_at=row.updated_at or previous.updated_at,
            )
        rows[row.key] = replace(row, title=row.title or "Current conversation")
    ordered = sorted(rows.values(), key=lambda row: row.updated_at, reverse=True)
    return sorted(ordered, key=lambda row: (not row.current, not bool(row.pane_id)))


def session_age(timestamp: str) -> str:
    """Render malformed/legacy dates honestly, without failing a whole list."""
    if not timestamp:
        return "Live"
    try:
        instant = datetime.fromisoformat(timestamp)
        instant = instant.replace(tzinfo=UTC) if instant.tzinfo is None else instant
        seconds = max(0, int((datetime.now(UTC) - instant).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return "Unknown"
    for divisor, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= divisor:
            return f"{seconds // divisor}{unit} ago"
    return f"{seconds}s ago"


class CommandCenterScreen(ModalScreen[CommandSession | None]):
    """A live, keyboard-first overview with explicit opening through existing flows."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close", priority=True),
        Binding("tab", "filter(1)", "Next filter", priority=True),
        Binding("shift+tab", "filter(-1)", "Previous filter", priority=True),
        Binding("ctrl+f", "search", "Search", priority=True),
        Binding("g", "group", "Group"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "details", "Details"),
        Binding("down", "focus_list", "Sessions"),
    ]
    DEFAULT_CSS = """
    CommandCenterScreen { background: $background; }
    CommandCenterScreen #cc-shell { height: 100%; padding: 1 2 0 2; }
    CommandCenterScreen #cc-header { height: 1; }
    CommandCenterScreen #cc-title { width: 24; text-style: bold; }
    CommandCenterScreen #cc-group { width: 1fr; color: $text-muted; }
    CommandCenterScreen #cc-filters { height: 2; border-bottom: solid $border; }
    CommandCenterScreen #cc-filters Button {
        width: auto; min-width: 8; height: 1; padding: 0 1; margin: 0 1 0 0;
        border: none; background: transparent; color: $text-muted;
    }
    CommandCenterScreen #cc-filters Button.selected { background: $accent 30%; color: $text; }
    CommandCenterScreen #cc-search { height: 3; display: none; }
    CommandCenterScreen #cc-notice { height: auto; color: $text-muted; }
    CommandCenterScreen #cc-body { height: 1fr; min-height: 1; }
    CommandCenterScreen #cc-list-column { width: 7fr; }
    CommandCenterScreen #cc-columns { height: 1; color: $text-muted; }
    CommandCenterScreen #cc-list { height: 1fr; border: none; padding: 0; background: transparent; }
    CommandCenterScreen #cc-list > .option-list--option { padding: 0; }
    CommandCenterScreen #cc-list > .option-list--option-disabled { color: $text-muted; }
    CommandCenterScreen #cc-list > .option-list--option-highlighted {
        background: $accent 35%; color: $text;
    }
    CommandCenterScreen #cc-empty { height: auto; padding: 2 1; color: $text-muted; }
    CommandCenterScreen #cc-details {
        width: 3fr; margin-left: 1; padding-left: 2; border-left: solid $border;
    }
    CommandCenterScreen #cc-detail-text { height: auto; }
    CommandCenterScreen #cc-footer { height: 2; padding-top: 1; color: $text-muted; }
    CommandCenterScreen.cc-narrow #cc-details { display: none; }
    CommandCenterScreen.cc-narrow #cc-filters Button { min-width: 0; margin: 0; }
    CommandCenterScreen.cc-details-only #cc-list-column { display: none; }
    CommandCenterScreen.cc-details-only #cc-details { display: block; width: 1fr; border: none; }
    CommandCenterScreen.cc-short #cc-shell { padding: 0 1; }
    CommandCenterScreen.cc-short #cc-filters { height: 2; }
    CommandCenterScreen.cc-short #cc-footer { height: 1; padding: 0; }
    """

    def __init__(
        self,
        manager: SessionManager | None,
        live: Callable[[], Sequence[CommandSession]],
    ) -> None:
        """Keep disk reads in workers and defer all navigation to the owning app."""
        super().__init__()
        self._manager = manager
        self._live = live
        self._saved: list[SessionMeta] = []
        self._external: list[CommandSession] = []
        self._rows: dict[str, CommandSession] = {}
        self._filter_index = 0
        self._group_index = 0
        self._limit = _PAGE_SIZE
        self._has_more = False
        self._loading = True
        self._error = ""
        self._detail_key: str | None = None
        self._preview: tuple[str, str] = ("", "")
        self._preview_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        """Match the reference's flat project list and right-hand detail column."""
        with Vertical(id="cc-shell"):
            with Horizontal(id="cc-header"):
                yield Static("Agent command center", id="cc-title")
                yield Static("Group: Project  g", id="cc-group")
            with Horizontal(id="cc-filters"):
                for index, status in enumerate(STATUSES):
                    yield Button(status, id=f"cc-filter-{index}")
            yield Input(placeholder="Search task, project, model or session…", id="cc-search")
            yield Static("Loading saved sessions…", id="cc-notice", markup=False)
            with Horizontal(id="cc-body"):
                with Vertical(id="cc-list-column"):
                    yield Static("Tasks", id="cc-columns")
                    yield OptionList(id="cc-list")
                    yield Static("", id="cc-empty", markup=False)
                with VerticalScroll(id="cc-details"):
                    yield Static("Task details", id="cc-detail-text", markup=False)
            yield Static(
                "↑/↓ select · Enter open · Tab filter · g group · Ctrl+F search · "
                "d details · r refresh · Esc close",
                id="cc-footer",
            )

    def on_mount(self) -> None:
        """Show live panes immediately, then merge saved metadata asynchronously."""
        self._resize()
        self._paint()
        self.query_one("#cc-list", OptionList).focus()
        self._load_saved()
        self._load_presence()
        self.set_interval(1, self._load_presence)
        self.set_interval(1, self._paint)

    @work(exclusive=True, group="command-center-presence")
    async def _load_presence(self) -> None:
        from pathlib import Path

        from novacode_cli.tui.presence import read_presence

        directory = getattr(self._manager, "sessions_dir", None)
        if not isinstance(directory, Path):
            return
        rows = await asyncio.to_thread(read_presence, directory)
        external = []
        for row in rows:
            # Only public display fields cross the process boundary. Never
            # trust another process's pane ID as a local navigation target.
            identity = row.get("session_id")
            if not isinstance(identity, str) or not _safe_identity(identity):
                continue
            status = row.get("status")
            if status not in STATUSES[1:]:
                continue
            fields = {
                key: str(row.get(key, ""))[:_DETAIL_CHARS]
                for key in ("title", "project", "model", "branch", "updated_at")
            }
            external.append(
                CommandSession(
                    key=f"session:{identity}",
                    session_id=identity,
                    status=status,
                    external=True,
                    title=fields["title"],
                    project=fields["project"],
                    model=fields["model"],
                    branch=fields["branch"],
                    updated_at=fields["updated_at"],
                )
            )
        self._external = external
        self._paint()

    def on_resize(self, _event: events.Resize) -> None:
        """Keep details reachable in small terminals through the d key."""
        if self.is_mounted:
            self._resize()
            self._paint()

    def _resize(self) -> None:
        self.set_class(self.size.width < _NARROW_WIDTH, "cc-narrow")
        self.set_class(self.size.height < 24, "cc-short")  # noqa: PLR2004 - compact layout boundary
        self.query_one("#cc-footer", Static).update(
            "Enter open · Tab filter · d details · Esc close"
            if self.size.width < _TINY_WIDTH
            else "Enter open · Tab filter · Ctrl+F search · d details · Esc close"
            if self.size.width < _NARROW_WIDTH
            else "↑/↓ select · Enter open · Tab filter · g group · Ctrl+F search · "
            "d details · r refresh · Esc close"
        )

    @work(exclusive=True, group="command-center-saved")
    async def _load_saved(self) -> None:
        self._loading = True
        self._error = ""
        self._paint()
        try:
            saved = (
                await asyncio.to_thread(self._manager.list_sessions, limit=self._limit + 1)
                if self._manager is not None
                else []
            )
        except Exception as exc:  # noqa: BLE001 - browsing must preserve live panes on storage failure
            logger.warning("Command Center could not load saved sessions: %s", type(exc).__name__)
            self._error = "Saved sessions could not be loaded. Press r to retry."
        else:
            self._saved = saved[: self._limit]
            self._has_more = len(saved) > self._limit
            self._detail_key = None
        finally:
            self._loading = False
        self._paint()

    def _selected(self) -> CommandSession | None:
        listing = self.query_one("#cc-list", OptionList)
        index = listing.highlighted
        if index is None or index >= listing.option_count:
            return None
        return self._rows.get(listing.get_option_at_index(index).id or "")

    def _line(self, title: str, status: str, age: str) -> Text:
        width = max(24, self.query_one("#cc-list-column").size.width)
        age_width = 12 if width >= 60 else 0  # noqa: PLR2004 - hide age before truncating task names
        name_width = max(8, width - 14 - age_width)
        line = Text(" ".join(title.split()), no_wrap=True, overflow="ellipsis")
        line.truncate(name_width, overflow="ellipsis", pad=True)
        line.append(f" {status:<12}")
        if age_width:
            line.append(f"{age:>11}")
        return line

    def _filtered_rows(self, rows: list[CommandSession]) -> list[CommandSession]:
        terms = self.query_one("#cc-search", Input).value.casefold().split()
        matches = [
            row
            for row in rows
            if all(
                term in f"{row.title} {row.project} {row.model} {row.session_id}".casefold()
                for term in terms
            )
        ]
        for index, status in enumerate(STATUSES):
            button = self.query_one(f"#cc-filter-{index}", Button)
            count = len(matches) if index == 0 else sum(row.status == status for row in matches)
            button.label = f"{status} {count}"
            button.set_class(index == self._filter_index, "selected")
        chosen = STATUSES[self._filter_index]
        return [row for row in matches if chosen in ("All", row.status)]

    def _paint(self) -> None:
        if not self.is_mounted:
            return
        listing = self.query_one("#cc-list", OptionList)
        previous = self._selected()
        scroll_y = listing.scroll_y
        rows = merge_sessions(self._saved, [*self._external, *self._live()])
        self._rows = {row.key: row for row in rows}
        visible = self._filtered_rows(rows)
        group = ("Project", "Status", "None")[self._group_index]
        self.query_one("#cc-group", Static).update(f"Group: {group}  g")

        def group_key(row: CommandSession) -> str:
            return row.project or "No project" if group == "Project" else row.status

        if group != "None":
            current_project = next((row.project for row in rows if row.current), None)
            if group == "Project":
                visible.sort(key=lambda row: (row.project != current_project, group_key(row)))
            else:
                visible.sort(key=lambda row: STATUSES.index(row.status))
        listing.clear_options()
        groups = groupby(visible, group_key) if group != "None" else [("", visible)]
        first = restore = None
        preferred = (
            previous.key
            if previous
            else self._detail_key or next((row.key for row in rows if row.current), None)
        )
        for label, group_members in groups:
            members = list(group_members)
            if label:
                if listing.option_count:
                    listing.add_option(Option("", disabled=True))
                listing.add_option(
                    Option(Text(f"{label}  {len(members)}", style="dim"), disabled=True)
                )
            for row in members:
                position = listing.option_count
                listing.add_option(
                    Option(
                        self._line(
                            f"● {row.title}" if row.pane_id else f"○ {row.title}",
                            row.status,
                            session_age(row.updated_at),
                        ),
                        id=row.key,
                    )
                )
                first = position if first is None else first
                if row.key == preferred:
                    restore = position
        if self._has_more:
            if first is None:
                first = listing.option_count
            listing.add_option(Option("Show more saved sessions…", id="cc-more"))
        listing.highlighted = restore if restore is not None else first
        listing.scroll_to(y=scroll_y, animate=False)
        self.query_one("#cc-columns", Static).update(self._line("Tasks", "Status", "Updated"))
        empty = self.query_one("#cc-empty", Static)
        empty.display = not visible
        empty.update(
            "Loading…"
            if self._loading
            else "No matching sessions. Try All, change your search, or start a conversation."
        )
        notice = self.query_one("#cc-notice", Static)
        notice.display = self._loading or bool(self._error)
        notice.update(self._error or "Loading saved sessions…")
        self._update_details()

    def _update_details(self) -> None:
        row = self._selected()
        if row is None:
            self.query_one("#cc-detail-text", Static).update(
                "Task details\n\nSelect a session to inspect it."
            )
            return
        if row.key != self._detail_key:
            self._detail_key = row.key
            self._preview = ("", "")
            self._load_preview(row)
        prompt, reply = self._preview
        text = Text("Task details\n\n", style="bold")
        text.append(row.title + "\n", style="bold")
        color = {"Working": "green", "Needs you": "yellow", "Ready": "cyan"}.get(row.status, "dim")
        text.append("● ", style=color)
        text.append(f"{row.status}\n\n")
        values = [
            ("Model", row.model or "Not recorded"),
            ("Reasoning", row.reasoning or "Not recorded"),
            ("Project", row.project or "No project recorded"),
            ("Session", row.session_id or "Unsaved live tab"),
            ("Branch", row.branch or "Not recorded"),
            (
                "Tokens",
                f"{row.input_tokens:,} in · {row.output_tokens:,} out"
                if row.input_tokens is not None and row.output_tokens is not None
                else "Not recorded",
            ),
            ("Last message", row.last_message or reply or "No saved assistant text."),
            ("Prompt", row.prompt or prompt or "No saved prompt."),
        ]
        if row.blocked_reason:
            values.insert(0, ("Needs attention", row.blocked_reason))
        for label, value in values:
            text.append(label + "\n", style="dim")
            text.append(value + "\n\n", style="not bold")
        self.query_one("#cc-detail-text", Static).update(text)

    @work(group="command-center-preview")
    async def _load_preview(self, row: CommandSession) -> None:
        if (
            self._manager is None
            or not _safe_identity(row.session_id)
            or (row.last_message and row.prompt)
        ):
            return
        try:
            async with self._preview_lock:
                if self._detail_key != row.key:
                    return
                messages = await asyncio.to_thread(
                    self._manager.load_recent_messages, row.session_id
                )
        except Exception:  # noqa: BLE001 - unreadable history must not prevent browsing/opening
            return
        if self.is_mounted and self._detail_key == row.key:
            self._preview = message_preview(messages)
            self._update_details()

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        """Update details as the keyboard or mouse changes selection."""
        self._update_details()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Return a stable session identity or load the next metadata page."""
        event.stop()
        if event.option_id == "cc-more":
            self._limit += _PAGE_SIZE
            self._load_saved()
        elif (row := self._selected()) is not None:
            if row.external:
                self.notify("This session is open in another terminal. Continue there.")
            else:
                self.dismiss(row)

    def on_input_changed(self, _event: Input.Changed) -> None:
        """Apply search locally without rereading saved conversations."""
        self._paint()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        """Return to the matching sessions before opening one."""
        self.action_focus_list()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Give mouse filter clicks the same behavior as Tab."""
        event.stop()
        if event.button.id and event.button.id.startswith("cc-filter-"):
            self._filter_index = int(event.button.id.rsplit("-", 1)[1])
            self._paint()
            self.action_focus_list()

    def action_filter(self, direction: int) -> None:
        """Cycle filters in the same order as the screenshot."""
        self._filter_index = (self._filter_index + direction) % len(STATUSES)
        self._paint()
        self.action_focus_list()

    def action_group(self) -> None:
        """Switch project/status grouping without changing session state."""
        self._group_index = (self._group_index + 1) % 3
        self._paint()

    def action_refresh(self) -> None:
        """Explicitly reload saved metadata; live polling performs no disk I/O."""
        self._load_saved()

    def action_search(self) -> None:
        """Reveal a search field without spending a row when not in use."""
        search = self.query_one("#cc-search", Input)
        search.display = True
        search.focus()

    def action_focus_list(self) -> None:
        """Return keyboard navigation to the session list."""
        self.query_one("#cc-list", OptionList).focus()

    def action_details(self) -> None:
        """Make the details pane reachable even below the split-view width."""
        self.toggle_class("cc-details-only")
        if self.has_class("cc-details-only"):
            self.query_one("#cc-details", VerticalScroll).focus()
        else:
            self.action_focus_list()
        self.call_after_refresh(self._paint)

    def action_close(self) -> None:
        """Return from detail/search mode before dismissing the overview."""
        if self.has_class("cc-details-only"):
            self.remove_class("cc-details-only")
            self.action_focus_list()
            self.call_after_refresh(self._paint)
        elif self.query_one("#cc-search", Input).has_focus:
            search = self.query_one("#cc-search", Input)
            search.value = ""
            search.display = False
            self.action_focus_list()
        else:
            self.dismiss(None)
