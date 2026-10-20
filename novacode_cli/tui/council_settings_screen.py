"""Draft-based council editor: identities, provider models and personality presets."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from novacode_cli.council_settings import (
    CouncilSettings,
    default_settings,
    parse_settings,
    personality_templates,
)
from novacode_cli.tui.animations import animate_modal_screen

if TYPE_CHECKING:
    from textual.app import ComposeResult


class CouncilSettingsScreen(ModalScreen[dict | None]):
    """Edit a private draft; only Save returns settings for persistence."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    CouncilSettingsScreen #modal-box {
        width: 92%; max-width: 104; height: 90%; max-height: 44; padding: 1 2;
    }
    CouncilSettingsScreen #council-help { height: auto; margin-bottom: 1; color: $text-muted; }
    CouncilSettingsScreen #council-body { height: 1fr; min-height: 5; }
    CouncilSettingsScreen #council-members {
        width: 24; max-width: 32%; height: 1fr; margin-right: 2;
    }
    CouncilSettingsScreen #council-editor { width: 1fr; height: 1fr; padding-right: 1; }
    CouncilSettingsScreen .council-label { height: 1; margin-top: 1; }
    CouncilSettingsScreen Input, CouncilSettingsScreen Select { width: 1fr; }
    CouncilSettingsScreen #council-template-row { height: 3; }
    CouncilSettingsScreen #council-template-apply { width: 10; min-width: 10; margin-left: 1; }
    CouncilSettingsScreen #council-system { height: 12; min-height: 5; }
    CouncilSettingsScreen #council-error { height: auto; color: $error; }
    CouncilSettingsScreen #modal-buttons { height: 3; margin-top: 1; align-horizontal: right; }
    CouncilSettingsScreen #modal-buttons Button { min-width: 10; width: auto; margin-left: 1; }
    """

    def __init__(self, settings: CouncilSettings, *, warning: str = "") -> None:
        """Keep the loaded settings isolated from unsaved edits."""
        super().__init__()
        self._draft = settings.to_dict()
        self._templates = {p.id: p for p in personality_templates()}
        self._current = 0
        self._warning = warning

    def _entries(self) -> list[dict]:
        return [*self._draft["members"], self._draft["judge"]]

    def compose(self) -> ComposeResult:
        """Keep actions fixed below the scrollable member editor."""
        from novacode_cli.config.model_manager import MODEL_PRESETS

        providers = [
            ("Session model (inherit)", ""),
            *((str(preset["name"]), provider) for provider, preset in MODEL_PRESETS.items()),
        ]
        with Vertical(id="modal-box"):
            yield Static(Text("Council settings", style="bold"), id="modal-title")
            yield Static(
                "Choose a voice, edit its perspective and model, then Save. "
                "Changes apply to future runs. The web debate uses the first five voices; "
                "the sixth voice and judge also join planning councils.",
                id="council-help",
            )
            with Horizontal(id="council-body"):
                yield OptionList(
                    *(Option(Text(entry["name"]), id=entry["id"]) for entry in self._entries()),
                    id="council-members",
                )
                with VerticalScroll(id="council-editor"):
                    yield Static("Display name", classes="council-label")
                    yield Input(id="council-name", max_length=64)
                    yield Static("Model provider", classes="council-label")
                    yield Select(providers, value="", allow_blank=False, id="council-provider")
                    yield Input(
                        placeholder="Model ID (required for a provider override)",
                        id="council-model",
                    )
                    yield Static("Personality template", classes="council-label")
                    with Horizontal(id="council-template-row"):
                        yield Select(
                            [(p.name, p.id) for p in self._templates.values()],
                            value="architect",
                            allow_blank=False,
                            id="council-template",
                        )
                        yield Button("Apply", id="council-template-apply")
                    yield Static("System prompt / judge perspective", classes="council-label")
                    yield TextArea(id="council-system", soft_wrap=True)
            yield Static(self._warning, id="council-error")
            with Horizontal(id="modal-buttons"):
                yield Button("Reset all", id="council-reset")
                yield Button("Cancel", id="council-cancel")
                yield Button("Save", id="council-save", variant="primary")

    def on_mount(self) -> None:
        """Render the first seat with the current theme's modal animation."""
        animate_modal_screen(self)
        self.query_one("#council-members", OptionList).highlighted = 0
        self._show_entry()
        if self._warning:
            self.query_one("#council-error", Static).update(self._warning)

    def _capture_entry(self) -> None:
        entry = self._entries()[self._current]
        entry["name"] = self.query_one("#council-name", Input).value
        entry["system"] = self.query_one("#council-system", TextArea).text
        provider = self.query_one("#council-provider", Select).value
        entry["provider"] = str(provider) if provider is not Select.BLANK else ""
        entry["model"] = self.query_one("#council-model", Input).value if entry["provider"] else ""
        self.query_one("#council-members", OptionList).replace_option_prompt(
            entry["id"], Text(entry["name"] or "Unnamed voice")
        )

    def _show_entry(self) -> None:
        entry = self._entries()[self._current]
        self.query_one("#council-name", Input).value = entry["name"]
        provider = self.query_one("#council-provider", Select)
        with provider.prevent(Select.Changed):
            provider.value = entry["provider"]
        model = self.query_one("#council-model", Input)
        model.value, model.disabled = entry["model"], not bool(entry["provider"])
        self.query_one("#council-system", TextArea).load_text(entry["system"])
        self.query_one("#council-error", Static).update("")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Keep unsaved edits when moving between voices."""
        if event.option_list.id != "council-members" or event.option_index == self._current:
            return
        self._capture_entry()
        self._current = event.option_index
        self._show_entry()

    def on_select_changed(self, event: Select.Changed) -> None:
        """An explicit provider change requires an explicit model ID."""
        if event.select.id == "council-provider":
            if event.value != event.select.value:
                return
            model = self.query_one("#council-model", Input)
            model.disabled = event.value == ""
            model.value = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Apply templates to the draft; validate the whole council before Save."""
        if event.button.id == "council-template-apply":
            template = self._templates[str(self.query_one("#council-template", Select).value)]
            name = template.name + (
                " (Judge)" if self._entries()[self._current]["id"] == "judge" else ""
            )
            self.query_one("#council-name", Input).value = name
            self.query_one("#council-system", TextArea).load_text(template.system)
        elif event.button.id == "council-reset":
            self._draft = default_settings().to_dict()
            members = self.query_one("#council-members", OptionList)
            for entry in self._entries():
                members.replace_option_prompt(entry["id"], Text(entry["name"]))
            self._show_entry()
        elif event.button.id == "council-save":
            self._capture_entry()
            try:
                result = parse_settings(self._draft)
            except ValueError as exc:
                self.query_one("#council-error", Static).update(str(exc))
                return
            self.dismiss(result.to_dict())
        elif event.button.id == "council-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        """Discard the draft, including applied templates or resets."""
        self.dismiss(None)
