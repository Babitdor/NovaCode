"""Manage reusable agents and run them outside the active conversation."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime

from rich.text import Text
from textual import on
from textual.containers import Grid, VerticalScroll
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
    ProactiveScreen > VerticalScroll { padding: 0 2 1 2; }
    ProactiveScreen Label { margin-top: 1; }
    ProactiveScreen TextArea { height: 7; }
    ProactiveScreen Grid { height: auto; grid-size: 2; grid-rows: 3; }
    ProactiveScreen Button { width: 100%; min-width: 0; }
    ProactiveScreen #proactive-report { height: auto; padding: 1 0; }
    """

    def __init__(self, store: ProactiveStore, workspace: str):
        super().__init__()
        self.store = store
        self.workspace = workspace
        self.definitions = {}
        self.key = None
        self.runs = {}

    def compose(self):
        with VerticalScroll():
            yield Label("Proactive agents")
            yield Select([("Create agent", "")], allow_blank=False, value="", id="proactive-agent")
            yield Label("Name")
            yield Input(id="proactive-name", max_length=80)
            yield Label("Standing instructions")
            yield TextArea(id="proactive-instructions")
            yield Label("Workspace")
            yield Input(self.workspace, id="proactive-workspace")
            yield Checkbox("Allow reads from committed Git files", id="proactive-read-workspace")
            yield Label("GitHub repository (optional owner/repo)")
            yield Input(id="proactive-github")
            yield Label("PyPI package (optional)")
            yield Input(id="proactive-pypi")
            yield Label("Cron schedule (optional, local time)")
            yield Input(placeholder="0 9 * * 1-5", id="proactive-cron")
            yield Checkbox("Watch for new GitHub tags", id="proactive-watch-tags")
            yield Checkbox("Enable triggers while NovaCode is open", id="proactive-enabled")
            yield Static("", id="proactive-trigger-status", markup=False)
            yield Label("Run context (optional, e.g. a release or commit)")
            yield Input(id="proactive-context", max_length=4000)
            with Grid():
                yield Button("Save agent", id="proactive-save", variant="primary")
                yield Button("Run now", id="proactive-run", disabled=True)
                yield Button("Remove agent", id="proactive-remove", disabled=True)
                yield Button("Back", id="proactive-back")
            yield Label("Run history")
            yield Select([], prompt="Select a run", id="proactive-history")
            yield Static("", id="proactive-report", markup=False)

    async def on_mount(self):
        await self.reload_agents()
        self.set_interval(2, self.reload_history)

    async def reload_agents(self):
        self.definitions = dict(await asyncio.to_thread(self.store.agents))
        select = self.query_one("#proactive-agent", Select)
        with select.prevent(Select.Changed):
            select.set_options([("Create agent", ""), *(
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
            return
        self.runs = runs
        select = self.query_one("#proactive-history", Select)
        previous = select.value
        with select.prevent(Select.Changed):
            select.set_options([
                (f"{datetime.fromtimestamp(row['created']):%b %d %H:%M} · {row['trigger']} · {row['status']}", key)
                for key, row in runs.items()
            ])
            select.value = previous if previous in runs else next(iter(runs), Select.NULL)
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
        self.query_one("#proactive-report", Static).update(
            row["result"] or row["status"].capitalize() if row else ""
        )

    @on(Button.Pressed)
    async def handle_button(self, event):
        action = event.button.id
        try:
            if action == "proactive-back":
                self.app.pop_screen()
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
