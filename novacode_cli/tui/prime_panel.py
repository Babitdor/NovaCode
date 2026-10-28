"""Compact Prime strip and a named, keyboard-accessible saved-work browser."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from novacode_cli.prime import receipt
from novacode_cli.prime_memory import (
    PrimeMemory,
    Record,
    freshness,
    project_context,
    session_directory,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

COMPACT_WIDTH = 80
logger = logging.getLogger(__name__)


def _saved_records(
    session: Any,  # noqa: ANN401 - Session boundary.
    kind: str,
    identity: str = "",
) -> list[Record]:
    """Read named rows in a worker, keeping store access and fingerprints off the UI loop."""
    memory = PrimeMemory(project_context(session_directory(session)))
    rows = []
    records = memory.records(kind)
    if identity and not any(record.get("id") == identity for record in records):
        requested = memory.get(kind, identity)
        if requested is not None:
            records.append(requested)
    for record in records:
        label = " ".join(str(record.get("goal") or record.get("purpose") or "Untitled").split())[
            :150
        ]
        state = freshness(memory.context["root"], record.get("fingerprints", {}))
        status = str(record.get("status", "unverified"))
        if status == "running":
            status = "paused"
        if kind == "checks" and status == "passed" and state != "current":
            status = state
        rows.append(
            {
                **record,
                "_label": label,
                "_kind": kind,
                "_freshness": state,
                "_status": status.replace("_", " ").capitalize(),
            }
        )
    return rows


def _record_details(session: Any, record: Record) -> str:  # noqa: ANN401 - Session boundary.
    """Present task context or captured check evidence in readable text, without ID plumbing."""
    title = str(record.get("goal") or record.get("purpose") or record["_label"])
    lines = [title, "", "Status: " + record["_status"]]
    if record["_kind"] == "tasks":
        lines.extend(
            ["Phase: " + str(record.get("phase", "define")).title(), "", "Completion criteria:"]
        )
        lines.extend("- " + str(item) for item in record.get("criteria", []))
        lines.extend(["", "Findings:", *[str(item) for item in record.get("findings", [])]])
        if record.get("questions"):
            lines.extend(["", "Unresolved questions:", *record["questions"]])
        lines.extend(
            [
                "",
                "Next step: " + str(record.get("next_action", "Review the saved task")),
                "Workspace: " + str(record.get("cwd", "Unknown")),
                "",
                "Load task prepares this context. Continue starts work in your current workspace.",
            ]
        )
    else:
        lines.extend(
            [
                "Inputs: " + record["_freshness"],
                "Finding: " + str(record.get("finding", "draft")),
                "",
                "Command: " + str(record.get("command", "")),
                "Expected exit code: " + str(record.get("expected_exit", 0)),
            ]
        )
        lines.extend("Required output: " + str(item) for item in record.get("contains", []))
        memory = PrimeMemory(project_context(session_directory(session)))
        evidence = [memory.get("evidence", key) for key in record.get("evidence_ids", [])]
        lines.extend(["", "Observed results:" if evidence else "No execution evidence saved yet."])
        for result in evidence:
            if result is not None:
                lines.extend(
                    [
                        str(result.get("status", "unverified")).capitalize()
                        + " · exit "
                        + str(result.get("exit_code", "unknown")),
                        str(result.get("reason", "")),
                        str(result.get("excerpt", "")),
                        "",
                    ]
                )
    return "\n".join(lines)


class PrimePanel(Vertical):
    """Browse saved tasks/checks by name; selecting and reading never starts agent work."""

    DEFAULT_CSS = """
    PrimePanel { height: auto; display: none; }
    PrimePanel #prime-strip {
        width: 100%; height: 1; min-height: 1; border: none;
        content-align: left middle; color: $accent; background: $surface;
    }
    PrimePanel #prime-strip:focus { background: $accent 20%; text-style: bold; }
    PrimePanel #prime-drawer { display: none; height: 13; border: solid $accent; }
    PrimePanel #prime-tabs, PrimePanel #prime-controls { height: 1; }
    PrimePanel #prime-tabs Button, PrimePanel #prime-controls Button {
        width: 1fr; min-width: 7; height: 1; min-height: 1; border: none;
        padding: 0; margin: 0; content-align: center middle;
    }
    PrimePanel #prime-tabs Button:focus, PrimePanel #prime-controls Button:focus {
        background: $accent 20%; text-style: bold;
    }
    PrimePanel #prime-tabs Button.active { color: $accent; text-style: bold; }
    PrimePanel #prime-body { height: 1fr; min-height: 1; }
    PrimePanel #prime-records { height: 1fr; min-height: 1; border: none; padding: 0; }
    PrimePanel #prime-records .option-list--option-highlighted {
        background: $accent 20%; color: $text; text-style: bold;
    }
    PrimePanel #prime-content-scroll { height: 1fr; padding: 0 1; display: none; }
    PrimePanel #prime-details { height: auto; }
    PrimePanel #prime-hint { height: 1; color: $text; padding: 0 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual widget identity.
        """Keep each browser's selection and pending reads scoped to its own widget."""
        super().__init__(id=id)
        self._view = "tasks"
        self._rows: dict[str, Record] = {}
        self._session: Any = None
        self._detail = False
        self._loading = False
        self._load_revision = 0
        self._option_signature: list[tuple[str, str, str]] | None = None

    class Control(Message):
        """A local user action dispatched by the owning app through existing controls."""

        def __init__(self, action: str, identity: str = "") -> None:
            """Carry the selected record internally, without asking users to enter an ID."""
            super().__init__()
            self.action = action
            self.identity = identity

    def compose(self) -> ComposeResult:
        """Keep browsing and details outside the transcript and composer draft."""
        yield Button("Prime · Define · details", id="prime-strip")
        with Vertical(id="prime-drawer"):
            with Horizontal(id="prime-tabs"):
                yield Button("Tasks", id="prime-tasks", classes="active")
                yield Button("Checks", id="prime-checks")
                yield Button("Progress", id="prime-progress")
                yield Button("Back", id="prime-back")
            with Vertical(id="prime-body"):
                yield OptionList(id="prime-records")
                with VerticalScroll(id="prime-content-scroll"):
                    yield Static("", id="prime-details", markup=False)
            yield Static("Select a saved task", id="prime-hint", markup=False)
            with Horizontal(id="prime-controls"):
                yield Button(
                    "Load task",
                    id="prime-prepare",
                    disabled=True,
                    tooltip="Load the selected task's context. Continue starts work.",
                )
                yield Button(
                    "Details",
                    id="prime-inspect",
                    disabled=True,
                    tooltip="Read the selected task or check without executing commands.",
                )
                yield Button(
                    "Remove",
                    id="prime-forget",
                    disabled=True,
                    tooltip="Remove the selected saved record after confirmation.",
                )
                yield Button(
                    "Continue",
                    id="prime-continue",
                    disabled=True,
                    tooltip="Start the loaded task in the current workspace and model.",
                )
                yield Button("Close", id="prime-close")

    def selected_record(self) -> Record | None:
        """Resolve the highlighted row, retaining its real identity only inside the UI."""
        options = self.query_one("#prime-records", OptionList)
        if self._view == "progress" or options.highlighted is None or not options.option_count:
            return None
        return self._rows.get(options.get_option_at_index(options.highlighted).id or "")

    def record_label(self, identity: str) -> str:
        """Name the selected record in local confirmation and success messages."""
        return str(self._rows.get(identity, {}).get("_label", "saved Prime record"))

    def _sync_controls(self) -> None:
        record = self.selected_record()
        self.query_one("#prime-prepare", Button).disabled = (
            self._loading or not record or self._view != "tasks"
        )
        for selector in ("#prime-inspect", "#prime-forget"):
            self.query_one(selector, Button).disabled = self._loading or record is None
        pending = getattr(self._session, "prime_pending", None)
        self.query_one("#prime-continue", Button).disabled = not bool(pending)
        self.query_one("#prime-back", Button).display = self._detail and self._view != "progress"
        self.query_one("#prime-records").display = not self._detail and self._view != "progress"
        self.query_one("#prime-content-scroll").display = self._detail or self._view == "progress"
        for view in ("tasks", "checks", "progress"):
            self.query_one("#prime-" + view).set_class(self._view == view, "active")
        if self._loading:
            hint = "Loading saved " + self._view + "..."
        elif pending:
            hint = "Loaded: " + str(pending.get("goal", "Saved task")) + " · Continue to start"
        elif self._view == "progress":
            hint = "Progress and verification from the current request"
        elif not self._rows:
            hint = (
                "No saved tasks. Enable Prime and send a task."
                if self._view == "tasks"
                else "No saved checks. Checks appear when Prime records verification."
            )
        else:
            hint = (
                "Select a task · Load task prepares it"
                if self._view == "tasks"
                else "Select a check · Details shows results"
            )
        self.query_one("#prime-hint", Static).update(Text(hint, no_wrap=True, overflow="ellipsis"))

    def update_report(self, session: Any) -> None:  # noqa: ANN401 - Session boundary.
        """Update the compact strip without changing transcript scrolling or opening details."""
        session_changed = self._session is not session
        self._session = session
        enabled = bool(getattr(session, "prime_enabled", False))
        report = getattr(session, "prime_report", None)
        if (
            getattr(self, "_last_report", None) is report
            and getattr(self, "_last_enabled", None) == enabled
            and getattr(self, "_last_width", None) == self.app.size.width
            and not session_changed
        ):
            return
        self._last_report, self._last_enabled, self._last_width = (
            report,
            enabled,
            self.app.size.width,
        )
        self.display = enabled or self.query_one("#prime-drawer").display
        source_report = report
        report = report or {}
        counts = {
            status: sum(c.get("status") == status for c in report.get("checks", []))
            for status in ("passed", "failed", "unverified")
        }
        label = (
            f"Prime · {report.get('goal', 'New task')[:16]} · "
            f"{report.get('phase', 'define').title()} · "
            f"{len(report.get('recalled', []))} recalled · "
            f"{counts['passed']} passed / {counts['failed']} failed / "
            f"{counts['unverified']} unverified"
        )
        compact = (
            f"Prime · {report.get('goal', 'New task')[:8]} · "
            f"{report.get('phase', 'define')[:3].title()} · R:{len(report.get('recalled', []))} "
            f"P:{counts['passed']} F:{counts['failed']} ?:{counts['unverified']}"
        )
        strip = self.query_one("#prime-strip", Button)
        strip.label = compact if self.app.size.width < COMPACT_WIDTH else label
        strip.tooltip = label + " · Open saved work"
        self.query_one("#prime-prepare", Button).label = (
            "Load" if self.app.size.width < COMPACT_WIDTH else "Load task"
        )
        self._sync_controls()
        # Layout-only updates must not supersede an explicit browser read.
        needs_records = (
            getattr(self, "_browser_report", None) is not source_report
            or getattr(self, "_browser_session", None) is not session
        )
        if (
            self.query_one("#prime-drawer").display
            and needs_records
            and (not self._loading or session_changed)
        ):
            self.run_worker(self.show_details(session), group="prime-details", exclusive=True)

    def on_resize(self) -> None:
        """Keep the drawer bounded when a terminal shrinks."""
        if self.is_mounted and self.display:
            session = getattr(self.app, "session_state", None)
            if session is not None:
                self.update_report(session)
            self.query_one("#prime-drawer").styles.height = min(
                13, max(7, self.app.size.height // 3)
            )

    def _populate_records(self, rows: list[Record], previous: str, identity: str) -> bool:
        """Publish named options and preserve selection; an explicit missing ID selects nothing."""
        self._rows = {record["id"]: record for record in rows}
        missing = bool(identity) and identity not in self._rows
        options = self.query_one("#prime-records", OptionList)
        signatures = [(row["id"], row["_label"], row["_status"]) for row in rows]
        if signatures != self._option_signature:
            options.clear_options().add_options(
                [
                    Option(
                        Text(
                            row["_status"] + " · " + row["_label"],
                            no_wrap=True,
                            overflow="ellipsis",
                        ),
                        id=row["id"],
                    )
                    for row in rows
                ]
            )
            self._option_signature = signatures
        options.highlighted = (
            next((i for i, row in enumerate(rows) if row["id"] == previous), 0)
            if rows and not missing
            else None
        )
        return missing

    async def show_details(
        self,
        session: Any,  # noqa: ANN401 - Session boundary.
        *,
        view: str | None = None,
        identity: str = "",
    ) -> None:
        """Refresh named records, fencing slow reads after tab or session changes."""
        self._session = session
        if view is not None:
            self._view, self._detail = view, False
        self._load_revision += 1
        revision, requested = self._load_revision, self._view
        report_at_start = getattr(session, "prime_report", None)
        selected = self.selected_record()
        previous = identity or (selected["id"] if selected else "")
        self._loading = True
        self._sync_controls()
        try:
            rows = (
                await asyncio.to_thread(_saved_records, session, requested, identity)
                if requested != "progress"
                else []
            )
            if revision != self._load_revision or session is not self._session:
                return
            missing = self._populate_records(rows, previous, identity)
            if identity:
                self._detail = not missing
            record = self.selected_record()
            if not record:
                self._detail = False
            if requested == "progress":
                content = await asyncio.to_thread(receipt, report_at_start or {})
            elif self._detail and record:
                content = await asyncio.to_thread(_record_details, session, record)
            else:
                content = ""
            if revision != self._load_revision or session is not self._session:
                return
            self.query_one("#prime-details", Static).update(content)
            self._browser_report, self._browser_session, self._loading = (
                report_at_start,
                session,
                False,
            )
            self._sync_controls()
            if missing:
                self.notify(
                    "Saved Prime check not found. Select a check from the list.", severity="warning"
                )
            if getattr(session, "prime_report", None) is not report_at_start:
                self.run_worker(self.show_details(session), group="prime-details", exclusive=True)
        except Exception:  # noqa: BLE001 - Local storage failure must not start work or crash the TUI.
            logger.warning("Prime saved-work browser could not load records", exc_info=True)
            if revision == self._load_revision and session is self._session:
                self._loading = False
                self._rows = {}
                self._option_signature = None
                self._browser_report, self._browser_session = report_at_start, session
                self.query_one("#prime-records", OptionList).clear_options()
                self._sync_controls()
                self.query_one("#prime-hint", Static).update(
                    "Prime storage unavailable. Try reopening saved work."
                )
                self.notify("Prime saved work could not be loaded.", severity="warning")

    @on(OptionList.OptionHighlighted, "#prime-records")
    def record_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Selecting a row enables its applicable actions without loading or executing a task."""
        event.stop()
        self._sync_controls()

    @on(OptionList.OptionSelected, "#prime-records")
    async def record_selected(self, event: OptionList.OptionSelected) -> None:
        """A click or Enter opens readable details; it never prepares or executes work."""
        event.stop()
        self._detail = True
        await self.show_details(self._session)
        self.query_one("#prime-content-scroll").focus()

    @on(Button.Pressed)
    async def control_pressed(self, event: Button.Pressed) -> None:
        """Dispatch selected records, with explicit Continue and confirmed removal."""
        event.stop()
        action = (event.button.id or "").removeprefix("prime-")
        if action in {"strip", "close"}:
            drawer = self.query_one("#prime-drawer")
            drawer.display = action == "strip" and not drawer.display
            drawer.styles.height = min(13, max(7, self.app.size.height // 3))
            if drawer.display:
                self.post_message(self.Control("open"))
                self.query_one("#prime-records").focus()
            else:
                self.display = bool(getattr(self._session, "prime_enabled", False))
                self.query_one("#prime-strip").focus()
            return
        if action in {"tasks", "checks", "progress"}:
            await self.show_details(self._session, view=action)
            self.query_one(
                "#prime-records" if action != "progress" else "#prime-content-scroll"
            ).focus()
        elif action == "back":
            self._detail = False
            self._sync_controls()
            self.query_one("#prime-records").focus()
        elif action == "inspect":
            self._detail = True
            await self.show_details(self._session)
            self.query_one("#prime-content-scroll").focus()
        else:
            record = self.selected_record()
            if action == "continue" or record:
                self.post_message(self.Control(action, record["id"] if record else ""))
