"""Oracle's composer strip, review threads, settings and owner decisions."""

from __future__ import annotations

import json

from rich.text import Text
from textual import on
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Static, Switch

from novacode_cli.core.oracle import MAX_ROUNDS, OracleRun, OracleSettings


class OracleSettingsScreen(ModalScreen[OracleSettings | None]):
    """Edit a copy; Cancel leaves both the profile and current thread untouched."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    OracleSettingsScreen { align: center middle; }
    OracleSettingsScreen #oracle-settings-box {
        width: 60; max-width: 95%; height: auto; max-height: 90%;
        border: solid $accent; background: $surface; padding: 1;
    }
    OracleSettingsScreen Input { height: 3; }
    OracleSettingsScreen Static { height: auto; }
    OracleSettingsScreen Horizontal { height: 3; }
    OracleSettingsScreen Button { width: 1fr; min-width: 8; }
    """

    def __init__(self, settings: OracleSettings):
        super().__init__()
        self.settings = settings

    def compose(self):
        with VerticalScroll(id="oracle-settings-box"):
            yield Static("Oracle settings", classes="title")
            yield Static("Uses your configured model. Each round makes one call per reviewer.")
            yield Static("Target score (greater than 0, up to 10)")
            yield Input(str(self.settings.target), id="oracle-target")
            yield Static(f"Maximum automatic review rounds (1–{MAX_ROUNDS})")
            yield Input(str(self.settings.max_rounds), id="oracle-rounds")
            yield Static("Independent reviewers (1–5)")
            yield Input(str(self.settings.reviewers), id="oracle-reviewers")
            yield Static("Start new threads with Oracle enabled")
            yield Switch(self.settings.default_enabled, id="oracle-default")
            yield Static("", id="oracle-settings-error", markup=False)
            with Horizontal():
                yield Button("Cancel", id="oracle-settings-cancel")
                yield Button("Save", id="oracle-settings-save", variant="primary")

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "oracle-settings-cancel":
            self.dismiss(None)
        elif event.button.id == "oracle-settings-save":
            try:
                value = OracleSettings(
                    target=float(self.query_one("#oracle-target", Input).value),
                    max_rounds=int(self.query_one("#oracle-rounds", Input).value),
                    reviewers=int(self.query_one("#oracle-reviewers", Input).value),
                    default_enabled=self.query_one("#oracle-default", Switch).value,
                )
            except ValueError:
                self.query_one("#oracle-settings-error", Static).update(
                    "Enter a target in (0, 10], rounds 1–20 and reviewers 1–5."
                )
                return
            self.dismiss(value)


class OracleThreadScreen(ModalScreen[None]):
    """Read one reviewer's independent conversations across rounds."""

    BINDINGS = [("escape", "close", "Back")]
    DEFAULT_CSS = """
    OracleThreadScreen > Vertical { background: $surface; }
    OracleThreadScreen Static { height: auto; padding: 1 2; }
    OracleThreadScreen VerticalScroll { height: 1fr; }
    """

    def __init__(self, run: OracleRun, slot: int):
        super().__init__()
        self.run, self.slot = run, slot

    def compose(self):
        with Vertical():
            yield Static(f"Oracle · Reviewer {self.slot} · independent, read-only threads")
            with VerticalScroll():
                for row in self.run.reviewers:
                    if row.slot == self.slot:
                        result = (
                            json.dumps(row.review.model_dump(), indent=2, ensure_ascii=False)
                            if row.review
                            else row.error or "Review running"
                        )
                        yield Static(
                            Text(
                                f"Round {row.round} · independent review\n\n"
                                f"Review input\n{row.prompt}\n\nReview result\n{result}"
                            )
                        )
            yield Button("Back", id="oracle-thread-back")

    def action_close(self):
        self.dismiss(None)

    @on(Button.Pressed, "#oracle-thread-back")
    def back(self):
        self.dismiss(None)


class OraclePanel(Vertical):
    """A one-line receipt; details expand without changing the composer's draft."""

    DEFAULT_CSS = """
    OraclePanel { height: auto; }
    OraclePanel #oracle-header { height: 1; }
    OraclePanel Button {
        height: 1; min-height: 1; border: none; padding: 0 1;
        background: $surface; color: $text; min-width: 5;
    }
    OraclePanel Button:hover, OraclePanel Button:focus { background: $accent 20%; }
    OraclePanel #oracle-strip { width: 1fr; content-align: left middle; color: $accent; }
    OraclePanel #oracle-settings { width: 10; }
    OraclePanel #oracle-drawer { height: 10; display: none; border: solid $accent; }
    OraclePanel Collapsible { height: auto; padding: 0; border: none; }
    OraclePanel Static { height: auto; }
    OraclePanel #oracle-error { color: $warning; }
    OraclePanel #oracle-actions {
        height: 4; grid-size: 3 2; grid-columns: 1fr 1fr 1fr; grid-rows: 2 2;
    }
    OraclePanel #oracle-lower-target {
        height: 1; min-height: 1; border: none; padding: 0; width: 100%;
    }
    OraclePanel #oracle-actions Button { width: 100%; }
    """

    class Control(Message):
        def __init__(self, action: str, value: float | None = None):
            super().__init__()
            self.action, self.value = action, value

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.run = None
        self._shown = {}

    def compose(self):
        with Horizontal(id="oracle-header"):
            yield Button(
                "Oracle: off",
                id="oracle-toggle",
                tooltip="Independent scored reviews for subsequent requests",
            )
            yield Button("Oracle · ready", id="oracle-strip")
            yield Button("Settings", id="oracle-settings")
        yield Static("", id="oracle-error", markup=False)
        with VerticalScroll(id="oracle-drawer"):
            for slot in range(1, 6):
                with Collapsible(title=f"Reviewer {slot}", id=f"oracle-row-{slot}"):
                    yield Static("", id=f"oracle-result-{slot}", markup=False)
                    yield Button("Open reviewer thread", id=f"oracle-thread-{slot}")
        with Grid(id="oracle-actions"):
            yield Button("Keep fixing", id="oracle-fix")
            yield Button("More rounds", id="oracle-extend")
            yield Button("Accept and finish", id="oracle-accept")
            yield Input("", placeholder="Lower target", id="oracle-lower-target")
            yield Button("Lower target", id="oracle-lower")
            yield Button("Pause", id="oracle-pause")

    def update_report(self, session):
        enabled = getattr(session, "oracle_enabled", False)
        self.display = True
        if not self.is_mounted:
            return
        run = getattr(session, "oracle_run", None)
        self.run = run
        settings = getattr(session, "oracle_settings", None) or OracleSettings()
        text = f"Oracle · ready · target {settings.target:g}"
        if run is not None:
            score = "—" if run.lowest is None else f"{run.lowest:g}/10"
            if run.lowest is not None and run.stale:
                score += " stale"
            text = (
                f"Oracle · {run.round}/{run.settings.max_rounds} · low {score}"
                f" · target {run.settings.target:g} · {run.status.replace('_', ' ')}"
            )
        strip = self.query_one("#oracle-strip", Button)
        strip.display = enabled
        if str(strip.label) != text:
            strip.label = Text(text)
        error = run.error if run else ""
        self.query_one("#oracle-error", Static).display = enabled and bool(error)
        if self._shown.get("error") != error:
            self._shown["error"] = error
            self.query_one("#oracle-error", Static).update(error)
        waiting = (
            enabled and run is not None and run.decision is not None and not run.decision.done()
        )
        if not enabled:
            self.query_one("#oracle-drawer").display = False
        self.query_one("#oracle-actions").display = waiting
        for action in ("fix", "extend", "lower"):
            self.query_one(f"#oracle-{action}", Button).disabled = bool(
                not waiting
                or (action != "lower" and run and run.round >= MAX_ROUNDS)
                or (action == "lower" and run and run.lowest is None)
            )
        for slot in range(1, 6):
            row_widget = self.query_one(f"#oracle-row-{slot}", Collapsible)
            rows = [r for r in run.reviewers if r.slot == slot] if run else []
            row_widget.display = bool(rows)
            if not rows:
                continue
            first, final = rows[0], rows[-1]
            score = lambda row: f"{row.review.score:g}" if row.review else "—"
            title = f"Reviewer {slot} · first {score(first)} → latest {score(final)}"
            if run.stale:
                title += " · stale"
            row_widget.title = title
            details = (
                final.review.summary + "\n" + "\n".join(final.review.findings)
                if final.review
                else final.error or "Review running"
            )
            if self._shown.get(slot) != (final.identity, details):
                self._shown[slot] = (final.identity, details)
                self.query_one(f"#oracle-result-{slot}", Static).update(details)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed):
        identity = event.button.id or ""
        if not identity.startswith("oracle-") or identity == "oracle-toggle":
            return
        event.stop()
        action = identity.removeprefix("oracle-")
        if action == "strip":
            drawer = self.query_one("#oracle-drawer")
            drawer.display = not drawer.display
        elif action.startswith("thread-") and self.run:
            self.app.push_screen(OracleThreadScreen(self.run, int(action.split("-")[1])))
        elif action == "lower":
            try:
                target = float(self.query_one("#oracle-lower-target", Input).value)
                if self.run is None or not 0 < target < self.run.settings.target:
                    raise ValueError
            except ValueError:
                self.notify("Enter a positive target below the current target", severity="warning")
                return
            self.post_message(self.Control(action, target))
        else:
            self.post_message(self.Control(action))
