"""Profile teammate control panel; execution belongs to the detached worker."""

from __future__ import annotations

import json
import webbrowser
from typing import TYPE_CHECKING, ClassVar

from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from novacode_cli.teammates.service import request
from novacode_cli.tui.teammate_updates import task_markdown, usage_markdown

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Resize

COMPACT_TERMINAL_HEIGHT = 34


class TeammatePanel(Vertical):
    """Review exact pending input and durable task history before approving."""

    CSS = """
    TeammatesScreen { align: center middle; background: $background 65%; }
    TeammatesScreen #teammate-panel {
        width: 94%; max-width: 112; height: 92%;
        border: round $primary-muted; background: $surface;
    }
    TeammatesScreen #panel-header { height: 3; padding: 0 1; align-vertical: middle; }
    TeammatesScreen #teammate-title { width: 1fr; text-style: bold; padding: 1 0 0 1; }
    TeammatesScreen #close { width: 9; min-width: 9; margin: 0; border: none; }
    TeammatesScreen #state-strip { height: 4; background: $boost; padding: 0 2; }
    TeammatesScreen .state { width: 1fr; padding: 1 1 0 0; height: 4; color: $text; }
    TeammatesScreen #approval-status.waiting { color: $warning; text-style: bold; }
    TeammatesScreen #teammate-state { height: auto; color: $warning; padding: 0 2; }
    TeammatesScreen TabbedContent { height: 1fr; }
    TeammatesScreen TabPane { padding: 0; height: 1fr; }
    TeammatesScreen .tab-body { padding: 1 2; height: 1fr; }
    TeammatesScreen Horizontal.actions { height: 3; margin-bottom: 1; }
    TeammatesScreen #settings-actions { dock: bottom; background: $surface; margin-bottom: 0; }
    TeammatesScreen Button { min-width: 8; margin-right: 1; }
    TeammatesScreen .section-title { text-style: bold; margin: 1 0; }
    TeammatesScreen .hint { color: $foreground 80%; height: auto; margin-bottom: 1; }
    TeammatesScreen #teammate-name, TeammatesScreen #teammate-confirm,
    TeammatesScreen #teammate-model, TeammatesScreen #teammate-vision-model {
        height: 3; min-height: 3; margin-bottom: 1; padding: 0 1;
        border: round $foreground 45%; background: $panel; color: $text;
    }
    TeammatesScreen #teammate-task, TeammatesScreen #teammate-role {
        height: 6; min-height: 6; margin-bottom: 1; padding: 0 1;
        border: round $foreground 45%; background: $panel; color: $text;
    }
    TeammatesScreen #teammate-task { height: 7; }
    TeammatesScreen #teammate-name:focus, TeammatesScreen #teammate-confirm:focus,
    TeammatesScreen #teammate-model:focus, TeammatesScreen #teammate-vision-model:focus,
    TeammatesScreen #teammate-task:focus, TeammatesScreen #teammate-role:focus {
        border: round $primary; background: $panel;
    }
    TeammatesScreen #approval-box { height: auto; padding: 0 1; margin-top: 1; background: $boost; }
    TeammatesScreen #approval-box.waiting { border: round $warning; }
    TeammatesScreen #approval-actions { height: 3; padding: 0 2; background: $boost; }
    TeammatesScreen #approval-box .section-title { margin: 0 0 1 0; }
    TeammatesScreen #teammate-approval { height: auto; margin-bottom: 1; }
    TeammatesScreen #teammate-history { height: auto; margin: 1 0; }
    TeammatesScreen #history-usage { height: auto; color: $foreground 80%; margin-top: 1; }
    TeammatesScreen #agent-usage { height: auto; margin-bottom: 1; }
    TeammatesScreen #usage-details { height: auto; }
    TeammatesScreen #delete { color: $error; }
    TeammatesScreen #teammate-error { color: $error; height: auto; padding: 0 2; }
    TeammatesScreen #panel-footer { height: 2; padding: 0 2; color: $foreground 80%; }
    TeammatesScreen.compact #teammate-panel { height: 98%; }
    TeammatesScreen.compact #state-strip { height: 3; }
    TeammatesScreen.compact .state { height: 3; padding-top: 0; }
    TeammatesScreen.compact #work-controls { height: 1; }
    TeammatesScreen.compact #work-controls Button {
        height: 1; min-height: 1; border: none; padding: 0 1;
    }
    """

    def __init__(self, agent_id: str | None = None, state: dict | None = None) -> None:
        """Retain selection across refreshes, never across input proposals."""
        super().__init__()
        self.agent_id = agent_id
        self.initial_state = state or {}
        self.pending: str | None = None
        self.command_pending = False
        self.loaded = False
        self.refreshing = False
        self.initial_view = False
        self.tasks: list[dict] = []
        self.history_options: list[tuple[Text, str]] = []
        self.teammate_name = "Teammate"

    def compose(self) -> ComposeResult:  # noqa: PLR0915 - Declarative tab layout.
        """Use labelled native controls with scrollable task and approval detail."""
        from novacode_cli.config.model_manager import MODEL_PRESETS

        providers = [(preset["name"], key) for key, preset in MODEL_PRESETS.items()]
        with Horizontal(id="state-strip"):
            for name in ("task", "computer", "control", "approval"):
                yield Static(f"{name.title()}\n—", id=f"{name}-status", classes="state")
        yield Static("Connecting to your teammate…", id="teammate-state", markup=False)
        with TabbedContent(initial="work-tab", id="teammate-tabs"):
            with TabPane("Work", id="work-tab"):
                with VerticalScroll(classes="tab-body"):
                    with Horizontal(id="work-controls", classes="actions"):
                        yield Button("Computer", id="preview", variant="primary")
                        yield Button("Pause", id="pause")
                        yield Button("Resume", id="resume")
                        yield Button("Stop", id="stop")
                    with Vertical(id="approval-box"):
                        yield Label("Desktop input", classes="section-title")
                        yield Static(
                            "No input waiting for review.", id="teammate-approval", markup=False
                        )
                    yield Label("Assign a task", classes="section-title")
                    yield TextArea(
                        id="teammate-task",
                        placeholder="Describe what you want your teammate to do…",
                        highlight_cursor_line=False,
                    )
                    yield Button("Assign task", id="assign", variant="primary")
                with Horizontal(id="approval-actions"):
                    yield Button("Approve once", id="approve", variant="success", disabled=True)
                    yield Button("Deny", id="deny", disabled=True)
            with TabPane("History", id="history-tab"), VerticalScroll(classes="tab-body"):
                yield Label("Task history", id="history-heading", classes="section-title")
                with Collapsible(title="Agent usage across all tasks", collapsed=True):
                    yield Static("", id="agent-usage", markup=False)
                yield Select([], prompt="No tasks yet", id="history-task")
                yield Static(
                    "Assigned tasks and their results appear here.",
                    id="teammate-history",
                    markup=False,
                )
                yield Static("", id="history-usage", markup=False)
                with Collapsible(title="Usage details", collapsed=True):
                    yield Static("", id="usage-details", markup=False)
            with TabPane("Settings", id="settings-tab"), VerticalScroll(classes="tab-body"):
                yield Label("Your teammate", classes="section-title")
                yield Static(
                    "An independent assistant with its own computer and retained conversation.",
                    classes="hint",
                )
                yield Label("Agent name")
                yield Input(placeholder="Name your teammate, e.g. Ada", id="teammate-name")
                yield Label("Role")
                yield TextArea(
                    id="teammate-role",
                    placeholder="Describe responsibilities and preferred working style…",
                    highlight_cursor_line=False,
                )
                yield Label("Model & provider", classes="section-title")
                yield Static(
                    "Pause before changing models. Uses your existing /auth credentials. "
                    "Leave blank on creation to use Nova's model.",
                    classes="hint",
                )
                yield Label("Provider")
                yield Select(providers, prompt="Use Nova's provider", id="teammate-provider")
                yield Label("Model ID")
                yield Input(
                    id="teammate-model",
                    placeholder="Choose a provider, then enter a model ID",
                    classes="model-id",
                )
                with Collapsible(title="Vision model for text-only assistants", collapsed=True):
                    yield Label("Vision provider")
                    yield Select(
                        providers,
                        prompt="Use Nova's vision provider",
                        id="teammate-vision-provider",
                    )
                    yield Label("Vision model ID")
                    yield Input(
                        id="teammate-vision-model",
                        placeholder="Vision-capable model ID",
                        classes="model-id",
                    )
                with Horizontal(id="settings-actions", classes="actions"):
                    yield Button("Create teammate", id="create", variant="primary")
                    yield Button("Save changes", id="edit", variant="primary")
                with Collapsible(title="Delete teammate", collapsed=True):
                    yield Static(
                        "Permanently remove this teammate, its computer, and history. "
                        "Enter its exact name to confirm.",
                        classes="hint",
                    )
                    yield Input(id="teammate-confirm", placeholder="Teammate name")
                    yield Button("Delete", id="delete")
        yield Static("", id="teammate-error", markup=False)

    def on_mount(self) -> None:
        """Initialize this agent's controls without replacing later drafts."""
        self.update_status(self.initial_state)

    def update_status(self, result: dict) -> None:
        """Refresh only this agent's history, pending approval, and power state."""
        self.teammate_name = result.get("name") or "Teammate"
        computer = result.get("computer") or {}
        pending = next((a for a in result.get("approvals", []) if a["state"] == "pending"), None)
        states = {
            "task": result.get("task_state", "idle"),
            "computer": computer.get("phase", "not created"),
            "control": "You" if computer.get("manual") else "Teammate",
            "approval": "Review input" if pending else "None pending",
        }
        for name, value in states.items():
            self.query_one(f"#{name}-status", Static).update(
                Text.assemble((f"{name.title()}\n", "dim"), (value.capitalize(), "bold"))
            )
        self.query_one("#approval-status").set_class(bool(pending), "waiting")
        recovery = result.get("recovery", "")
        self.query_one("#teammate-state", Static).update(recovery)
        self.query_one("#teammate-state").display = bool(recovery)
        if not self.initial_view:
            if not result.get("name"):
                self.query_one("#teammate-tabs", TabbedContent).active = "settings-tab"
            self.initial_view = True
        if not self.loaded and result.get("name"):
            self._load_settings(result)
        self.query_one("#create", Button).disabled = bool(result.get("name"))
        self.query_one("#create").display = not result.get("name")
        self.query_one("#edit").display = bool(result.get("name"))
        for selector in (
            "#edit",
            "#assign",
            "#preview",
            "#pause",
            "#resume",
            "#stop",
            "#delete",
        ):
            self.query_one(selector, Button).disabled = not result.get("name")
        self.pending = pending["id"] if pending else None
        self.query_one("#approval-box").set_class(bool(pending), "waiting")
        self.query_one("#approval-box").display = bool(pending)
        self.query_one("#approval-actions").display = bool(pending)
        self.query_one("#teammate-approval", Static).update(
            "Review the saved observation in Computer before approving.\n"
            f"Task: {pending.get('task', '')}\n"
            f"Observation: {pending.get('screenshot', '')}\n\n"
            + json.dumps(pending.get("args", {}), indent=2)
            if pending
            else "No input waiting for review. Desktop actions appear here for your approval."
        )
        for selector in ("#approve", "#deny"):
            self.query_one(selector, Button).disabled = not pending
        self.tasks = result.get("tasks", [])
        options = [
            (Text(f"{t['status'].capitalize()} · {' '.join(t['text'].split())[:60]}"), t["id"])
            for t in self.tasks
        ]
        if options != self.history_options:
            selector = self.query_one("#history-task", Select)
            selected = selector.value
            selector.set_options(options)
            if options:
                selector.value = (
                    selected if any(key == selected for _, key in options) else options[-1][1]
                )
            self.history_options = options
        self._show_history()

    def _load_settings(self, result: dict) -> None:
        """Populate saved settings once, preserving drafts on later refreshes."""
        self.query_one("#teammate-name", Input).value = result["name"]
        self.query_one("#teammate-role", TextArea).load_text(result["role"])
        for field, suffix in (("model", ""), ("vision", "vision-")):
            selected = result.get(field) or {}
            provider = self.query_one(f"#teammate-{suffix}provider", Select)
            with provider.prevent(Select.Changed):
                if selected.get("provider"):
                    provider.value = selected["provider"]
                else:
                    provider.clear()
            self.query_one(f"#teammate-{suffix}model", Input).value = selected.get("model", "")
        self.loaded = True

    def on_select_changed(self, event: Select.Changed) -> None:
        """Keep older tasks selectable as newer assignments arrive."""
        if event.select.id == "history-task":
            self._show_history()
        elif event.select.id in {"teammate-provider", "teammate-vision-provider"}:
            if not isinstance(event.value, str):
                return
            from novacode_cli.config.model_manager import MODEL_PRESETS

            suffix = "vision-" if event.select.id == "teammate-vision-provider" else ""
            self.query_one(f"#teammate-{suffix}model", Input).value = MODEL_PRESETS[event.value][
                "default_model"
            ]

    def _show_history(self) -> None:
        self.query_one("#history-heading", Label).update(f"{self.teammate_name} · Task history")
        agent_summary, _ = usage_markdown(self.tasks)
        self.query_one("#agent-usage", Static).update(Markdown(agent_summary))
        selected = self.query_one("#history-task", Select).value
        task = next((t for t in self.tasks if t["id"] == selected), None)
        if task is None:
            self.query_one("#teammate-history", Static).update(
                "No tasks yet. Assign your first task from Work."
            )
            self.query_one("#history-usage", Static).update("")
            self.query_one("#usage-details", Static).update("")
            return
        self.query_one("#teammate-history", Static).update(Markdown(task_markdown(task)))
        summary, details = usage_markdown([task])
        self.query_one("#history-usage", Static).update(Markdown(summary))
        self.query_one("#usage-details", Static).update(Markdown(details))

    def _clear_creation_form(self) -> None:
        """Prepare a blank new-agent form without replacing any mounted panel."""
        for editor in self.query(Input):
            editor.value = ""
        for editor in self.query(TextArea):
            editor.clear()
        for selector in self.query(Select):
            with selector.prevent(Select.Changed):
                selector.clear()
        self.query_one("#teammate-tabs", TabbedContent).active = "settings-tab"

    @work(group="teammate-control")
    async def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: PLR0912
        """Send explicit user intent; never infer approval from a refresh."""
        action = event.button.id
        event.stop()
        if self.command_pending:
            return
        payload: dict = {}
        if action in {"create", "edit"}:
            payload["name"] = self.query_one("#teammate-name", Input).value
            if action == "edit" or self.query_one("#teammate-role", TextArea).text:
                payload["role"] = self.query_one("#teammate-role", TextArea).text
            for field, suffix in (("model", ""), ("vision", "vision-")):
                provider = self.query_one(f"#teammate-{suffix}provider", Select).value
                model = self.query_one(f"#teammate-{suffix}model", Input).value.strip()
                if isinstance(provider, str) or model:
                    payload[field] = {
                        "provider": provider if isinstance(provider, str) else "",
                        "model": model,
                    }
        elif action == "assign":
            payload["text"] = self.query_one("#teammate-task", TextArea).text
        elif action in {"approve", "deny"}:
            payload = {"id": self.pending, "approve": action == "approve"}
            action = "decide"
        elif action == "delete":
            payload["confirm"] = self.query_one("#teammate-confirm", Input).value
        if self.agent_id is not None and action != "create":
            payload["agent_id"] = self.agent_id
        self.command_pending = True
        event.button.disabled = True
        try:
            result = await request(str(action), payload)
            self.query_one("#teammate-error", Static).update("")
            if action == "preview":
                import asyncio

                await asyncio.to_thread(webbrowser.open, result["url"])
            elif action == "assign":
                self.query_one("#teammate-task", TextArea).clear()
            elif action == "delete":
                self.loaded = False
                self.initial_view = False
                self.query_one("#teammate-confirm", Input).value = ""
            elif action == "create":
                self.screen.created_id = result.get("agent_id")
                self._clear_creation_form()
            self.screen.reload()
        except Exception as exc:  # noqa: BLE001 - Keep panel open for retry.
            self.query_one("#teammate-error", Static).update(str(exc))
        finally:
            self.command_pending = False
            if self.is_mounted:
                event.button.disabled = False


class TeammatesScreen(ModalScreen[None]):
    """One named tab per teammate, with independent editor and review state."""

    BINDINGS: ClassVar = [("escape", "close", "Close")]
    CSS = (
        TeammatePanel.CSS
        + """
    TeammatesScreen TeammatePanel { height: 1fr; }
    TeammatesScreen #agent-tabs { height: 1fr; }
    TeammatesScreen #agent-tabs > ContentSwitcher > TabPane { padding: 0; }
    TeammatesScreen #roster-error { height: auto; color: $error; padding: 0 2; }
    """
    )

    def __init__(self) -> None:
        """Retain mounted panels so tab switches preserve unsent edits."""
        super().__init__()
        self.panels: dict[str, TeammatePanel] = {}
        self.refreshing = False
        self.initial_selection = False
        self.created_id: str | None = None

    def compose(self) -> ComposeResult:
        """Keep roster navigation separate from each agent's Work/History/Settings."""
        with Vertical(id="teammate-panel"):
            with Horizontal(id="panel-header"):
                yield Static("Teammates", id="teammate-title", markup=False)
                yield Button("Close", id="close")
            with (
                TabbedContent(initial="agent-new", id="agent-tabs"),
                TabPane("+ New teammate", id="agent-new"),
            ):
                yield TeammatePanel()
            yield Static("", id="roster-error", markup=False)
            yield Static(
                "Esc close · Assigned work continues after you disconnect.", id="panel-footer"
            )

    def on_mount(self) -> None:
        """Reconnect without owning the detached worker's lifetime."""
        self.reload()
        self.set_interval(2, self.reload)

    def on_resize(self, event: Resize) -> None:
        """Keep review controls usable on shorter terminals."""
        self.set_class(event.size.height < COMPACT_TERMINAL_HEIGHT, "compact")

    def action_close(self) -> None:
        """Disconnect without stopping any teammate."""
        self.workers.cancel_group(self, "teammate-status")
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """The header owns Close; individual panels own all agent actions."""
        if event.button.id == "close":
            self.action_close()

    @work(group="teammate-status")
    async def reload(self) -> None:
        """Refresh the roster while preserving each existing panel and selection."""
        if self.refreshing:
            return
        self.refreshing = True
        try:
            result = await request("status")
            roster = result.get("agents")
            if roster is None:
                roster = [result] if result.get("name") else []
            tabs = self.query_one("#agent-tabs", TabbedContent)
            present = set()
            for state in roster:
                key = state.get("agent_id") or state.get("thread") or "legacy"
                present.add(key)
                pane_id = f"agent-{key}"
                if key not in self.panels:
                    panel = TeammatePanel(state.get("agent_id"), state)
                    await tabs.add_pane(
                        TabPane(escape(state["name"]), panel, id=pane_id), before="agent-new"
                    )
                    self.panels[key] = panel
                else:
                    self.panels[key].update_status(state)
                    tabs.get_tab(pane_id).label = escape(state["name"])
            for key in list(self.panels):
                if key not in present:
                    await tabs.remove_pane(f"agent-{key}")
                    del self.panels[key]
            if self.created_id in self.panels:
                tabs.active = f"agent-{self.created_id}"
                self.created_id = None
            elif not self.initial_selection:
                tabs.active = f"agent-{next(iter(self.panels))}" if self.panels else "agent-new"
            self.initial_selection = True
            self.query_one("#roster-error", Static).update("")
        except Exception as exc:  # noqa: BLE001 - Recoverable service or layout failure.
            if errors := self.query("#roster-error"):
                errors.first(Static).update(str(exc))
        finally:
            self.refreshing = False
