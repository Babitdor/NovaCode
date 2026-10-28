"""Compact foreground Prime strip and an independently scrollable details drawer."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Static

from novacode_cli.prime import receipt

if TYPE_CHECKING:
    from textual.app import ComposeResult

COMPACT_WIDTH = 80


class PrimePanel(Vertical):
    """Opening details or preparing context never starts an agent turn."""

    DEFAULT_CSS = """
    PrimePanel { height: auto; display: none; }
    PrimePanel #prime-strip {
        width: 100%; height: 1; min-height: 1; border: none;
        content-align: left middle; color: $accent; background: $surface;
    }
    PrimePanel #prime-strip:focus { background: $accent 20%; text-style: bold; }
    PrimePanel #prime-drawer { display: none; height: 13; border: solid $accent; }
    PrimePanel #prime-content-scroll { height: 1fr; padding: 0 1; }
    PrimePanel #prime-details { height: auto; }
    PrimePanel Input { height: 3; }
    PrimePanel #prime-controls { height: 1; }
    PrimePanel #prime-controls Button {
        width: 1fr; min-width: 7; height: 1; min-height: 1; border: none;
        padding: 0; margin: 0; content-align: center middle;
    }
    PrimePanel #prime-controls Button:focus { background: $accent 20%; text-style: bold; }
    """

    class Control(Message):
        """A local user action for the owning app to dispatch through normal controls."""

        def __init__(self, action: str, identity: str = "") -> None:
            """Describe the local action without starting work."""
            super().__init__()
            self.action = action
            self.identity = identity

    def compose(self) -> ComposeResult:
        """Keep details outside the transcript and the composer draft."""
        yield Button("Prime · Define · details", id="prime-strip")
        with Vertical(id="prime-drawer"):
            with VerticalScroll(id="prime-content-scroll"):
                yield Static("No saved Prime task yet.", id="prime-details", markup=False)
            yield Input(placeholder="Task or check ID", id="prime-record-id")
            with Horizontal(id="prime-controls"):
                yield Button("Prepare", id="prime-prepare")
                yield Button("Inspect", id="prime-inspect")
                yield Button("Forget", id="prime-forget")
                yield Button("Continue", id="prime-continue", disabled=True)
                yield Button("Close", id="prime-close")

    def update_report(self, session: Any) -> None:  # noqa: ANN401 - Session boundary.
        """Update counts without changing transcript scroll position or opening details."""
        enabled = bool(getattr(session, "prime_enabled", False))
        report = getattr(session, "prime_report", None)
        if (
            getattr(self, "_last_report", None) is report
            and getattr(self, "_last_enabled", None) == enabled
            and getattr(self, "_last_width", None) == self.app.size.width
        ):
            return
        self._last_report, self._last_enabled = report, enabled
        self._last_width = self.app.size.width
        self.display = enabled or self.query_one("#prime-drawer").display
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
            f"{report.get('phase', 'define')[:3].title()} · "
            f"R:{len(report.get('recalled', []))} "
            f"P:{counts['passed']} F:{counts['failed']} ?:{counts['unverified']}"
        )
        self.query_one("#prime-strip", Button).label = (
            compact if self.app.size.width < COMPACT_WIDTH else label
        )
        self.query_one("#prime-strip", Button).tooltip = label + " · Open details"
        self.query_one("#prime-continue", Button).disabled = not bool(
            getattr(session, "prime_pending", None)
        )
        if self.query_one("#prime-drawer").display:
            self.run_worker(self.show_details(session), group="prime-details", exclusive=True)

    def on_resize(self) -> None:
        """Keep the strip compact and details bounded when the terminal shrinks."""
        if self.is_mounted:
            session = getattr(self.app, "session_state", None)
            if session is not None:
                self.update_report(session)
            self.query_one("#prime-drawer").styles.height = min(
                13,
                max(7, self.app.size.height // 3),
            )

    async def show_details(self, session: Any, detail: str = "") -> None:  # noqa: ANN401
        """Load detailed evidence only in the explicitly opened, scrollable drawer."""
        from novacode_cli.commands.prime_handler import handle_prime_command

        report = getattr(session, "prime_report", None) or {}
        history, checks, assessment = await asyncio.gather(
            asyncio.to_thread(handle_prime_command, session, "tasks"),
            asyncio.to_thread(handle_prime_command, session, "checks"),
            asyncio.to_thread(receipt, report),
        )
        content = assessment if report else "No active Prime task."
        content += "\n\nTask history\n" + history + "\n\nRemembered checks\n" + checks
        if report:
            content += "\n\nCurrent definitions, findings and evidence\n" + json.dumps(
                {
                    key: report.get(key, [])
                    for key in (
                        "checks",
                        "evidence",
                        "findings",
                        "questions",
                        "next_action",
                        "continuation",
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        if detail:
            content += "\n\nSelected record\n" + detail
        self.query_one("#prime-details", Static).update(content)

    @on(Button.Pressed)
    def control_pressed(self, event: Button.Pressed) -> None:
        """Offer keyboard-accessible controls; execution is a separate explicit action."""
        event.stop()
        identity = self.query_one("#prime-record-id", Input).value.strip()
        action = (event.button.id or "").removeprefix("prime-")
        if action in {"strip", "close"}:
            drawer = self.query_one("#prime-drawer")
            drawer.display = action == "strip" and not drawer.display
            drawer.styles.height = min(13, max(7, self.app.size.height // 3))
            if drawer.display:
                self.post_message(self.Control("open"))
                self.query_one("#prime-content-scroll").focus()
            else:
                self.query_one("#prime-strip").focus()
            return
        self.post_message(self.Control(action, identity))
