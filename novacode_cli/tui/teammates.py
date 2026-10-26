"""Profile teammate control panel; execution belongs to the detached worker."""

from __future__ import annotations

import json
import webbrowser
from typing import TYPE_CHECKING, ClassVar

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

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Resize

COMPACT_TERMINAL_HEIGHT = 34


class TeammatesScreen(ModalScreen[None]):
    """Review exact pending input and durable task history before approving."""

    BINDINGS: ClassVar = [("escape", "close", "Close")]
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

    def __init__(self) -> None:
        """Retain selection across refreshes, never across input proposals."""
        super().__init__()
        self.pending: str | None = None
        self.loaded = False
        self.refreshing = False
        self.initial_view = False
        self.tasks: list[dict] = []
        self.history_options: list[tuple[Text, str]] = []

    def compose(self) -> ComposeResult:  # noqa: PLR0915 - Declarative tab layout.
        """Use labelled native controls with scrollable task and approval detail."""
        from novacode_cli.config.model_manager import MODEL_PRESETS

        providers = [(preset["name"], key) for key, preset in MODEL_PRESETS.items()]
        with Vertical(id="teammate-panel"):
            with Horizontal(id="panel-header"):
                yield Static("Teammates", id="teammate-title", markup=False)
                yield Button("Close", id="close")
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
                    yield Label("Task history", classes="section-title")
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
                        "One assistant. Its own computer and retained conversation.", classes="hint"
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
            yield Static(
                "Esc close · Assigned work continues after you disconnect.", id="panel-footer"
            )

    def on_mount(self) -> None:
        """Poll only while panel exists; closing never stops worker."""
        self.reload()
        self.set_interval(2, self.reload)

    def on_resize(self, event: Resize) -> None:
        """Keep review content visible on shorter terminals."""
        self.set_class(event.size.height < COMPACT_TERMINAL_HEIGHT, "compact")

    def action_close(self) -> None:
        """Disconnect interface while leaving execution alive."""
        self.dismiss(None)

    @work(group="teammate-status")
    async def reload(self) -> None:
        """Refresh status without replacing edits in progress."""
        if self.refreshing:
            return
        self.refreshing = True
        try:
            result = await request("status")
            computer = result.get("computer") or {}
            pending = next(
                (a for a in result.get("approvals", []) if a["state"] == "pending"), None
            )
            self.query_one("#teammate-title", Static).update(
                f"Teammates / {result['name']}" if result.get("name") else "Teammates"
            )
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
        except Exception as exc:  # noqa: BLE001 - Recoverable service error.
            self.query_one("#teammate-error", Static).update(str(exc))
        finally:
            self.refreshing = False

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
        selected = self.query_one("#history-task", Select).value
        task = next((t for t in self.tasks if t["id"] == selected), None)
        if task is None:
            self.query_one("#teammate-history", Static).update(
                "No tasks yet. Assign your first task from Work."
            )
            self.query_one("#history-usage", Static).update("")
            self.query_one("#usage-details", Static).update("")
            return
        self.query_one("#teammate-history", Static).update(
            f"{task['status'].capitalize()}\n\n{task['text']}\n\n"
            + str(task.get("error") or task.get("full_output") or "No result recorded yet.")
        )
        from novacode_cli.tracking.usage_tree import UsageTree

        history = task.get("usage_history") or (
            {"turn": task["usage"]} if task.get("usage") else {}
        )
        calls = tokens = 0
        cost = 0.0
        unknown_cost = False
        for usage in history.values():
            tree = UsageTree.from_dict(usage)
            totals = tree.root.totals()
            calls += totals.get("calls", 0)
            tokens += totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
            if totals.get("calls", 0):
                amount = tree.root.cost(recursive=True)
                unknown_cost |= amount is None
                cost += amount or 0
        price = "Cost unavailable" if unknown_cost else f"${cost:.4f}"
        self.query_one("#history-usage", Static).update(
            f"{calls:,} model {'call' if calls == 1 else 'calls'} · {tokens:,} tokens · {price}"
            if calls
            else "No model usage recorded."
        )
        self.query_one("#usage-details", Static).update(json.dumps(history, indent=2))

    @work(group="teammate-control", exclusive=True)
    async def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: PLR0912
        """Send explicit user intent; never infer approval from a refresh."""
        action = event.button.id
        if action == "close":
            self.action_close()
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
                self.query_one("#teammate-tabs", TabbedContent).active = "work-tab"
            self.reload()
        except Exception as exc:  # noqa: BLE001 - Keep panel open for retry.
            self.query_one("#teammate-error", Static).update(str(exc))
