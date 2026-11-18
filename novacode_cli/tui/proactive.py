"""Manage reusable agents and run them outside the active conversation."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime

from rich.text import Text
from textual import events, on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from novacode_cli.proactive import (
    RUN_TIMEOUT,
    AgentSpec,
    ProactiveStore,
    execute_run,
    github_tags,
)

logger = logging.getLogger(__name__)


class ProactiveRuntime:
    def __init__(self, store, model_factory, notify):
        self.store = store
        self.model_factory = model_factory
        self.notify = notify
        self.tasks = [asyncio.create_task(loop()) for loop in (self._runs, self._tags)]

    async def _runs(self):
        while True:
            try:
                job = await asyncio.to_thread(self.store.claim, time.time())
                if job is not None:
                    await self._run(job)
                    continue
            except sqlite3.Error:
                logger.exception("Proactive agent storage unavailable")
            await asyncio.sleep(2)

    async def _run(self, job):
        name = AgentSpec.model_validate_json(job["spec"]).name
        try:
            async with asyncio.timeout(RUN_TIMEOUT):
                model = await asyncio.to_thread(self.model_factory)
                result = await execute_run(job, model)
            status = "complete"
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish, job, "interrupted", "NovaCode closed.")
            raise
        except TimeoutError:
            status, result = "failed", "The agent reached its three-minute run limit."
        except Exception as exc:  # One failing agent cannot stop later runs.
            status, result = "failed", f"Run failed ({type(exc).__name__}). Check the model and tool setup."
        await asyncio.to_thread(self.store.finish, job, status, result)
        self.notify(f"{name} · {status}. Open /proactive to see the report.", title="Proactive agent")

    async def _tags(self):
        while True:
            try:
                watch = await asyncio.to_thread(self.store.tag_watch, time.time())
                if watch:
                    key, definition, repo = watch
                    try:
                        tags = await github_tags(repo)
                    except (OSError, ValueError, TimeoutError):
                        logger.warning("Proactive tag check failed for %s", repo)
                        await asyncio.to_thread(self.store.tag_failure, key, definition)
                    else:
                        await asyncio.to_thread(self.store.record_tags, key, definition, tags)
                    continue
            except sqlite3.Error:
                logger.exception("Proactive tag storage unavailable")
            await asyncio.sleep(5)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def start_proactive(app, *, create=False):
    from novacode_cli.config.config import settings
    from novacode_cli.config.model_create import create_model

    runtime = getattr(app, "_proactive_runtime", None)
    if runtime is not None:
        return runtime
    path = settings.user_deepagents_dir / "proactive.sqlite3"
    if create or path.is_file():
        try:
            store = await asyncio.to_thread(ProactiveStore, path)
        except (OSError, sqlite3.Error):
            logger.exception("Could not open proactive agent storage")
            app.notify("Could not open proactive agent storage.", severity="error")
            return None
        runtime = getattr(app, "_proactive_runtime", None)
        if runtime is None:
            runtime = app._proactive_runtime = ProactiveRuntime(store, create_model, app.notify)
    return runtime


class ProactiveScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]
    CSS = """
    ProactiveScreen { background: $background; }
    ProactiveScreen #proactive-header { height: 3; align-vertical: middle; padding: 0 1; }
    ProactiveScreen #proactive-title {
        width: 1fr; border-left: thick $accent; padding-left: 1;
        text-style: bold; color: $text;
    }
    ProactiveScreen #proactive-agent { margin: 0 1 1 1; width: 1fr; }
    ProactiveScreen #proactive-tabs { display: none; height: 2; margin: 0 1; }
    ProactiveScreen #proactive-tabs Button { width: 1fr; }
    ProactiveScreen.show-activity #proactive-configure-tab, ProactiveScreen #proactive-activity-tab {
        background: $surface; color: $text-muted;
    }
    ProactiveScreen #proactive-configure-tab,
    ProactiveScreen.show-activity #proactive-activity-tab {
        background: $primary-muted; color: $text; text-style: bold;
    }
    ProactiveScreen #proactive-tabs Button:focus { text-style: bold underline; }
    ProactiveScreen #proactive-body { height: 1fr; padding: 0 1; }
    ProactiveScreen #proactive-configure { width: 5fr; margin-right: 1; }
    ProactiveScreen #proactive-activity { width: 4fr; }
    ProactiveScreen .proactive-pane { height: 1fr; background: $surface; }
    ProactiveScreen .proactive-heading {
        height: 2; padding: 0 1; text-style: bold; color: $accent;
        border-bottom: solid $primary-muted;
    }
    ProactiveScreen .proactive-fields { height: 1fr; padding: 0 1 1 1; }
    ProactiveScreen Label { margin-top: 1; color: $text-muted; }
    ProactiveScreen .proactive-section {
        margin-top: 1; height: 1; color: $text; text-style: bold;
    }
    ProactiveScreen TextArea { height: 7; }
    ProactiveScreen Checkbox { width: 100%; padding: 0; }
    ProactiveScreen Button { min-width: 0; }
    ProactiveScreen #proactive-actions { height: 2; padding: 0 1; }
    ProactiveScreen #proactive-actions Button { width: 1fr; }
    ProactiveScreen #proactive-save { margin-right: 1; }
    ProactiveScreen #proactive-run-row { height: 3; align-vertical: middle; }
    ProactiveScreen #proactive-context { width: 1fr; }
    ProactiveScreen #proactive-run { width: 11; margin: 1 0 0 1; }
    ProactiveScreen #proactive-trigger-status { color: $text-muted; height: auto; margin-top: 1; }
    ProactiveScreen #proactive-report-status {
        text-style: bold; height: auto; margin-top: 1; padding-left: 1; color: $text;
        border-left: tall $primary-muted;
    }
    ProactiveScreen #proactive-report-status.complete { border-left: tall $success; }
    ProactiveScreen #proactive-report-status.failed { border-left: tall $error; }
    ProactiveScreen #proactive-report-status.queued,
    ProactiveScreen #proactive-report-status.running,
    ProactiveScreen #proactive-report-status.interrupted { border-left: tall $warning; }
    ProactiveScreen #proactive-report {
        height: auto; padding: 0 1; margin-top: 1; color: $text;
        border-left: tall $primary-muted;
    }
    ProactiveScreen #proactive-report.empty { color: $text-muted; }
    ProactiveScreen.compact #proactive-tabs { display: block; }
    ProactiveScreen.compact .proactive-heading { display: none; }
    ProactiveScreen.compact #proactive-configure,
    ProactiveScreen.compact #proactive-activity { width: 1fr; margin-right: 0; }
    ProactiveScreen.compact #proactive-activity,
    ProactiveScreen.compact.show-activity #proactive-configure { display: none; }
    ProactiveScreen.compact.show-activity #proactive-activity { display: block; }
    """

    def __init__(self, store: ProactiveStore, workspace: str):
        super().__init__()
        self.store = store
        self.workspace = workspace
        self.definitions = {}
        self.key = None
        self.runs = {}

    def compose(self):
        with Horizontal(id="proactive-header"):
            yield Static("Proactive agents", id="proactive-title")
            yield Button("Back", id="proactive-back", compact=True)
        yield Select([("New agent", "")], allow_blank=False, value="", id="proactive-agent")
        with Horizontal(id="proactive-tabs"):
            yield Button("Configure", id="proactive-configure-tab", compact=True)
            yield Button("Activity", id="proactive-activity-tab", compact=True)
        with Horizontal(id="proactive-body"):
            with Vertical(id="proactive-configure", classes="proactive-pane"):
                yield Static("Configure", classes="proactive-heading")
                with VerticalScroll(classes="proactive-fields"):
                    yield Label("Name")
                    yield Input(id="proactive-name", max_length=80)
                    yield Label("Standing instructions")
                    yield TextArea(id="proactive-instructions", soft_wrap=True)
                    yield Static("Sources", classes="proactive-section")
                    yield Label("Workspace")
                    yield Input(self.workspace, id="proactive-workspace")
                    yield Checkbox("Read committed Git files", id="proactive-read-workspace", compact=True)
                    yield Label("GitHub repository")
                    yield Input(id="proactive-github", placeholder="owner/repo (optional)")
                    yield Label("PyPI package")
                    yield Input(id="proactive-pypi", placeholder="package-name (optional)")
                    yield Static("Triggers", classes="proactive-section")
                    yield Label("Cron schedule (local time)")
                    yield Input(placeholder="0 9 * * 1-5 (optional)", id="proactive-cron")
                    yield Checkbox("Watch for new GitHub tags", id="proactive-watch-tags", compact=True)
                    yield Checkbox("Enable triggers while open", id="proactive-enabled", compact=True)
                    yield Static("", id="proactive-trigger-status", markup=False)
                with Horizontal(id="proactive-actions"):
                    yield Button("Save agent", id="proactive-save", variant="primary", compact=True)
                    yield Button("Remove", id="proactive-remove", disabled=True, compact=True)
            with Vertical(id="proactive-activity", classes="proactive-pane"):
                yield Static("Activity", classes="proactive-heading")
                with VerticalScroll(classes="proactive-fields"):
                    yield Label("Run context (optional)")
                    with Horizontal(id="proactive-run-row"):
                        yield Input(id="proactive-context", max_length=4000, placeholder="Release or commit")
                        yield Button("Run now", id="proactive-run", disabled=True, variant="primary", compact=True)
                    yield Label("Run history")
                    yield Select([], prompt="Select a run", id="proactive-history", disabled=True)
                    yield Static("", id="proactive-report-status", markup=False)
                    yield Static("", id="proactive-report", markup=False)

    async def on_mount(self):
        self.show_report(Select.NULL)
        await self.reload_agents()
        self.set_interval(2, self.reload_history)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 86, "compact")

    async def reload_agents(self):
        self.definitions = dict(await asyncio.to_thread(self.store.agents))
        select = self.query_one("#proactive-agent", Select)
        with select.prevent(Select.Changed):
            select.set_options([("New agent", ""), *(
                (Text(spec.name), key) for key, spec in self.definitions.items()
            )])
            select.value = self.key or ""
        self.query_one("#proactive-run", Button).disabled = self.key is None
        self.query_one("#proactive-remove", Button).disabled = self.key is None

    async def reload_history(self):
        key = self.key
        if key:
            history, triggers = await asyncio.gather(
                asyncio.to_thread(self.store.history, key),
                asyncio.to_thread(self.store.trigger_state, key),
            )
        else:
            history, triggers = [], {}
        if key != self.key:
            return
        self.show_triggers(triggers)
        runs = {row["id"]: row for row in history}
        if runs == self.runs:
            if not runs:
                self.show_report(Select.NULL)
            return
        self.runs = runs
        select = self.query_one("#proactive-history", Select)
        previous = select.value
        with select.prevent(Select.Changed):
            select.set_options([
                (f"{datetime.fromtimestamp(row['created']):%b %d %H:%M} · {row['status'].title()}", key)
                for key, row in runs.items()
            ])
            select.value = previous if previous in runs else next(iter(runs), Select.NULL)
            select.disabled = not runs
        self.show_report(select.value)

    def show_triggers(self, state):
        spec = self.definitions.get(self.key)
        lines = []
        if spec and not spec.enabled and (spec.cron or spec.watch_tags):
            lines.append("Triggers paused")
        if spec and spec.enabled and state.get("due") is not None:
            when = datetime.fromtimestamp(state["due"])
            lines.append(f"Next scheduled run · {when:%b %d %H:%M}")
        if spec and spec.enabled and spec.watch_tags:
            lines.append(state.get("tag_error") or (
                "Watching for new GitHub tags" if state.get("tag_ready") else "Waiting for first GitHub tag check"
            ))
        widget = self.query_one("#proactive-trigger-status", Static)
        text = "\n".join(lines)
        widget.display = bool(text)
        if str(widget.content) != text:
            widget.update(text)

    @on(Select.Changed, "#proactive-agent")
    async def select_agent(self, event):
        self.key = event.value if event.value in self.definitions else None
        if self.key is None:
            self.remove_class("show-activity")
        record = self.definitions.get(self.key)
        for field in ("name", "workspace", "github", "pypi", "cron"):
            value = getattr(record, field, self.workspace if field == "workspace" else "")
            self.query_one(f"#proactive-{field}", Input).value = value
        self.query_one("#proactive-instructions", TextArea).load_text(getattr(record, "instructions", ""))
        self.query_one("#proactive-context", Input).value = ""
        for field in ("read_workspace", "watch_tags", "enabled"):
            identity = field.replace("_", "-")
            self.query_one(f"#proactive-{identity}", Checkbox).value = getattr(record, field, False)
        self.query_one("#proactive-run", Button).disabled = self.key is None
        self.query_one("#proactive-remove", Button).disabled = self.key is None
        await self.reload_history()

    @on(Select.Changed, "#proactive-history")
    def select_run(self, event):
        self.show_report(event.value)

    def show_report(self, run_id):
        row = self.runs.get(run_id)
        status = self.query_one("#proactive-report-status", Static)
        report = self.query_one("#proactive-report", Static)
        status.display = row is not None
        report.set_class(not row or not row["result"], "empty")
        if row:
            when = datetime.fromtimestamp(row["created"])
            status.set_classes(row["status"])
            status.update(f"{row['status'].title()} · {row['trigger']} · {when:%b %d %H:%M}")
            report.update(row["result"] or "The report appears when the run ends.")
        else:
            empty = "No runs yet. Run this agent to see a report." if self.key else "Save an agent to start a run."
            if report.content != empty:
                report.update(empty)

    @on(Button.Pressed)
    async def handle_button(self, event):
        action = event.button.id
        try:
            if action == "proactive-back":
                self.app.pop_screen()
            elif action in {"proactive-configure-tab", "proactive-activity-tab"}:
                self.set_class(action == "proactive-activity-tab", "show-activity")
            elif action == "proactive-save":
                spec = AgentSpec(
                    **{name: self.query_one(f"#proactive-{name}", Input).value
                       for name in ("name", "workspace", "github", "pypi", "cron")},
                    instructions=self.query_one("#proactive-instructions", TextArea).text,
                    read_workspace=self.query_one("#proactive-read-workspace", Checkbox).value,
                    watch_tags=self.query_one("#proactive-watch-tags", Checkbox).value,
                    enabled=self.query_one("#proactive-enabled", Checkbox).value,
                )
                self.key = await asyncio.to_thread(self.store.save, self.key, spec)
                await self.reload_agents()
                await self.reload_history()
                self.notify("Agent saved.")
            elif action == "proactive-run" and self.key:
                context = self.query_one("#proactive-context", Input).value
                await asyncio.to_thread(self.store.queue, self.key, context)
                await self.reload_history()
                self.notify("Agent run queued using its saved settings.")
            elif action == "proactive-remove" and self.key:
                from novacode_cli.tui.screens import ConfirmModal

                key = self.key
                self.app.push_screen(
                    ConfirmModal("Remove proactive agent?", Text("This removes its triggers and run history.")),
                    lambda confirmed: self.remove_agent(key) if confirmed else None,
                )
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.notify(str(exc), severity="error")

    async def remove_agent(self, key):
        try:
            await asyncio.to_thread(self.store.remove, key)
        except (ValueError, sqlite3.Error) as exc:
            self.notify(str(exc), severity="error")
            return
        self.key = None
        await self.reload_agents()
        # Clear the removed definition, even when the select already shows Create agent.
        await self.select_agent(Select.Changed(self.query_one("#proactive-agent", Select), ""))
