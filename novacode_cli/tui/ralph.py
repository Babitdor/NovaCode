"""Inline progress and controls for an active Ralph run."""

from __future__ import annotations

import time

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from novacode_cli.commands import ralph_events as rev


class RalphProgress(Vertical):
    DEFAULT_CSS = """
    RalphProgress {
        height: auto; margin: 1 0; padding: 0 1;
        border-left: tall $accent; background: $surface;
    }
    RalphProgress Static { height: auto; }
    RalphProgress Horizontal { height: 2; }
    RalphProgress Button { min-width: 0; margin-right: 1; }
    """

    def __init__(self, run: rev.RalphStarted, session, cancel):
        super().__init__(classes="ralph-active")
        self.run = run
        self.session = session
        self.cancel = cancel
        self.event = None
        self.started = time.monotonic()
        self.request = ""
        self._timer = None

    def compose(self):
        yield Static(Text.assemble(("Ralph", "bold"), "  ·  ", self.run.task))
        yield Static(id="ralph-progress", markup=False)
        with Horizontal(id="ralph-controls"):
            yield Button("Pause", id="ralph-pause", compact=True)
            yield Button("Stop", id="ralph-stop", compact=True)

    def on_mount(self) -> None:
        self._timer = self.set_interval(1, self._refresh_status)
        self._refresh_status()

    def update_run(self, event) -> None:
        self.event = event
        if isinstance(event, rev.IterationStarted):
            self.started = time.monotonic()
        if self.is_mounted:
            self._refresh_status()

    def _refresh_status(self) -> None:
        event = self.event
        if isinstance(event, rev.RalphFinished):
            if self._timer is not None:
                self._timer.stop()
            self.remove_class("ralph-active")
            self.query_one("#ralph-controls", Horizontal).display = False
            status = Text(event.detail or event.reason.capitalize())
            status.append(f" · {event.completed} completed", style="dim")
        elif self.request:
            status = Text(self.request)
        elif isinstance(event, (rev.IterationStarted, rev.IterationFinished)):
            count = f"{event.iteration}/{event.max_iterations}" if event.max_iterations else str(event.iteration)
            elapsed = time.monotonic() - self.started
            phase = "Running" if isinstance(event, rev.IterationStarted) else "Checking results"
            status = Text(f"Iteration {count} · {phase} · {elapsed:.0f}s")
        else:
            status = Text("Starting…")
        self.query_one("#ralph-progress", Static).update(status)

    @on(Button.Pressed)
    def control(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "ralph-pause":
            self.session._ralph_checkpoint_requested = True
            self.request = "Pausing after this iteration…"
        else:
            self.session._ralph_stop_requested = True
            self.request = "Stopping after this iteration…" if self.run.background else "Stopping…"
            if not self.run.background:
                self.cancel()
        for button in self.query(Button):
            button.disabled = True
        self._refresh_status()
