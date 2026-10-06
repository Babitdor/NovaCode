"""Validated optional UI panels; the conversation shell always stays mounted."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import DirectoryTree, Static

if TYPE_CHECKING:
    from textual.app import App

PanelId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class PanelSpec(BaseModel):
    """One registered, non-executable component."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: PanelId
    type: Literal["notes", "files", "activity"]
    title: str = Field(default="", max_length=64)
    text: str = Field(default="", max_length=20000)
    height: int = Field(default=10, ge=3, le=30)
    visible: bool = True


class UISpec(BaseModel):
    """Versioned layout data, with no Python, CSS or arbitrary action strings."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    width: int = Field(default=28, ge=16, le=48)
    panels: list[PanelSpec] = Field(default_factory=list, max_length=4)
    theme: str | None = None
    bindings: dict[str, str] = Field(default_factory=dict)
    commands: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_actions(self) -> UISpec:
        """Only panel toggles may be bound; core controls cannot be replaced."""
        ids = {panel.id for panel in self.panels}
        if len(ids) != len(self.panels):
            message = "Panel IDs must be unique."
            raise ValueError(message)
        if any(key not in {f"f{i}" for i in range(6, 13)} for key in self.bindings):
            message = "Custom shortcuts must use F6-F12; core shortcuts are protected."
            raise ValueError(message)
        if any(not re.fullmatch(r"ui-[a-z][a-z0-9-]{0,28}", key) for key in self.commands):
            message = "Custom commands must start with ui-, such as ui-tests."
            raise ValueError(message)
        allowed = {f"toggle:{panel_id}" for panel_id in ids}
        if any(
            action not in allowed for action in [*self.bindings.values(), *self.commands.values()]
        ):
            message = "Actions must toggle a panel declared in this layout."
            raise ValueError(message)
        return self


class HarnessDock(VerticalScroll):
    """A bounded optional sidebar next to the protected session transcript."""

    DEFAULT_CSS = """
    HarnessDock {
        display: none; height: 1fr; max-width: 40%;
        border-left: solid $accent; padding: 0 1;
    }
    HarnessDock > VerticalScroll { height: auto; }
    .harness-panel { border: round $accent; margin-bottom: 1; padding: 0 1; }
    .harness-title { text-style: bold; color: $accent; }
    """


class UIHarnessRequest(Message):
    """Deliver an agent request to the host's serialized UI message handler."""

    def __init__(self, operation: str, payload: dict, future: asyncio.Future) -> None:
        """Capture the operation and the requesting event loop's response future."""
        super().__init__()
        self.operation = operation
        self.payload = payload
        self.future = future


def profile_path(workspace: Path) -> Path:
    """Store a separate user-owned layout for each workspace."""
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
    return Path.home() / ".nova" / "ui" / f"{key}.json"


class UIHarness:
    """Stage, preview, persist and restore optional panels without replacing core widgets."""

    def __init__(self, app: App, workspace: Path, path: Path | None = None) -> None:
        """Start with a default layout and a workspace-specific profile."""
        self.app = app
        self.workspace = workspace
        self.path = path or profile_path(workspace)
        self.committed = UISpec()
        self.candidate = self.committed.model_copy(deep=True)
        self.applied = self.committed.model_copy(deep=True)
        self.base_theme = app.theme
        self.activity: deque[str] = deque(maxlen=100)

    def inspect(self) -> dict:
        """The staged and rendered layouts, supported types and protected regions."""
        return {
            "candidate": self.candidate.model_dump(),
            "applied": self.applied.model_dump(),
            "committed": self.committed.model_dump(),
            "profile": str(self.path),
            "components": ["notes", "files", "activity"],
            "themes": sorted(self.app.available_themes),
            "protected": ["conversation", "prompt", "approvals", "sessions", "recovery"],
            "preview_active": self.applied != self.committed,
        }

    async def initialize(self) -> str:
        """Invalid profiles and safe startup both retain the built-in shell."""
        if os.environ.get("NOVA_SAFE_UI") == "1":
            return "Safe UI: saved customizations were skipped. Use /ui reset to clear them."
        try:
            if self.path.exists():
                spec = UISpec.model_validate_json(
                    await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                )
                await self.apply(spec)
                self.committed = spec
                self.candidate = spec.model_copy(deep=True)
        except (OSError, ValueError) as error:
            return f"UI profile could not load; using the default shell: {error}"
        return ""

    async def apply(self, spec: UISpec) -> None:
        """Mount a candidate offscreen before removing any existing optional panels."""
        if spec.theme is not None and spec.theme not in self.app.available_themes:
            message = f"Unknown theme: {spec.theme}"
            raise ValueError(message)
        dock = self.app.query_one("#ui-harness-dock", HarnessDock)
        stage = VerticalScroll()
        stage.display = False
        for panel in spec.panels:
            body = (
                DirectoryTree(str(self.workspace))
                if panel.type == "files"
                else Static(
                    "\n".join(self.activity) if panel.type == "activity" else panel.text,
                    markup=False,
                )
            )
            if panel.type == "activity":
                body.add_class("harness-activity")
            widget = VerticalScroll(
                Static(panel.title or panel.id, classes="harness-title", markup=False),
                body,
                id=f"harness-{panel.id}",
                classes="harness-panel",
            )
            widget.styles.height = panel.height
            widget.display = panel.visible
            stage.compose_add_child(widget)
        old = list(dock.children)
        try:
            await dock.mount(stage)
        except Exception:
            await stage.remove()
            raise
        for child in old:
            await child.remove()
        dock.styles.width = spec.width
        stage.display = True
        dock.display = any(panel.visible for panel in spec.panels)
        self.app.theme = spec.theme or self.base_theme
        self.applied = spec.model_copy(deep=True)

    async def execute(self, operation: str, payload: dict | None = None) -> dict:
        """Execute one validated operation in the host event loop."""
        payload = payload or {}
        if operation == "patch":
            merged = self.candidate.model_dump() | payload
            self.candidate = UISpec.model_validate(merged)
        elif operation == "preview":
            await self.apply(self.candidate)
        elif operation == "rollback":
            await self.apply(self.committed)
            self.candidate = self.committed.model_copy(deep=True)
        elif operation == "commit":
            await self.apply(self.candidate)
            await asyncio.to_thread(self._save, self.candidate)
            self.committed = self.candidate.model_copy(deep=True)
        elif operation == "reset":
            await self.apply(UISpec())
            await asyncio.to_thread(self.path.unlink, missing_ok=True)
            self.committed = UISpec()
            self.candidate = UISpec()
        elif operation != "inspect":
            message = f"Unknown UI operation: {operation}"
            raise ValueError(message)
        return self.inspect()

    def _save(self, spec: UISpec) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.stem}-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    async def toggle(self, action: str) -> None:
        """Toggle an optional panel as an unsaved preview."""
        spec = self.applied.model_copy(deep=True)
        for panel in spec.panels:
            if action == f"toggle:{panel.id}":
                panel.visible = not panel.visible
        self.candidate = spec
        await self.apply(spec)

    def observe(self, event: object) -> None:
        """Feed the activity component with tool and subagent status, never token deltas."""
        from novacode_cli import ui_events as ev

        line = ""
        if isinstance(event, ev.ToolCall):
            line = f"Started: {event.name}"
        elif isinstance(event, ev.ToolResult):
            line = f"{'Failed' if event.is_error else 'Finished'}: {event.call_id or 'tool'}"
        elif isinstance(event, ev.SubagentTask):
            line = f"{event.label or event.subagent_type}: {event.status}"
        if line:
            self.activity.append(line)
            for widget in self.app.query(".harness-activity"):
                if isinstance(widget, Static):
                    widget.update("\n".join(self.activity))
