"""Searchable, responsive command and evolution reference panels."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from novacode_cli.tui.palette import cached_palette

_GROUPS = {
    "Conversation": "help sessions import compare session resume save copy clear compact context tokens cost images files artifacts btw steer quit exit",
    "Models & settings": "model router auth settings theme effort voice update ui verbose",
    "Agents & automation": "subagents agents agent-server tasks cowork plan goal remote notifications cron webhook research ralph council servers kill tests browser-use trello",
    "Skills & knowledge": "init mcp skills plugins middleware reload-plugins prompt refine dream evolution reindex restore hooks create ingest ask file wiki learning",
    "Observability": "trace log",
}

_EXAMPLES = {
    "router": "/router  ·  switch setups and edit model routes",
    "context": "/context imported compact  ·  /context imported full  ·  /context imported relevant",
    "session": "/session new  ·  /session list  ·  /session close",
    "compare": "/compare claude:<id> codex:<id>",
    "resume": "/resume <session-id>",
    "steer": "/steer <instruction>  ·  You can also type naturally during a running turn.",
    "ui": "/ui inspect  ·  /ui reset",
    "learning": "/learning on  ·  /learning off  ·  /learning status",
}


@dataclass(frozen=True)
class HelpEntry:
    command: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    example: str = ""


def help_entries(
    registry: dict[str, Any], plugins=(), panel_commands=(), skills=()
) -> list[HelpEntry]:
    """Derive help from commands actually registered, preserving precedence."""
    entries = []
    seen = set(registry)
    seen.update(alias for spec in registry.values() for alias in spec.aliases)
    for name, spec in registry.items():
        category = next(
            (group for group, names in _GROUPS.items() if name in names.split()), "Other commands"
        )
        entries.append(
            HelpEntry(
                f"/{name}",
                spec.help,
                category,
                tuple(f"/{alias}" for alias in spec.aliases),
                _EXAMPLES.get(name, ""),
            )
        )
    for names, category, description in (
        (panel_commands, "UI panels", "Toggle a configured UI panel"),
        (plugins, "Plugins", "Run a command from an enabled plugin"),
        (skills, "Skills", "Read and pin this skill for the current conversation"),
    ):
        for name in sorted(names):
            if name not in seen:
                seen.add(name)
                entries.append(
                    HelpEntry(
                        f"/{name}",
                        description,
                        category,
                        (f"/skill:{name}",) if category == "Skills" else (),
                    )
                )
    entries.extend(
        [
            HelpEntry(
                "!<command>",
                "Run a shell command on the host",
                "Input & shortcuts",
                example="!git status",
            ),
            HelpEntry(
                "@path/to/file", "Attach a file reference to your prompt", "Input & shortcuts"
            ),
            HelpEntry(
                "/skill:<name>",
                "Invoke and pin a skill; /<skill-name> also works",
                "Input & shortcuts",
            ),
            HelpEntry("@<agent> <task>", "Delegate to a named subagent", "Input & shortcuts"),
            HelpEntry(
                "Ctrl+B",
                "Move a running shell or agent task into the background",
                "Input & shortcuts",
            ),
            HelpEntry("Esc", "Interrupt the current turn", "Input & shortcuts"),
            HelpEntry(
                "Ctrl+N · Alt+1…9",
                "Create a session / switch between sessions",
                "Input & shortcuts",
            ),
            HelpEntry("Ctrl+T", "Toggle the terminal", "Input & shortcuts"),
            HelpEntry("Alt+T · Alt+S", "Toggle todos / active subagents", "Input & shortcuts"),
            HelpEntry("Ctrl+End", "Jump to the latest output", "Input & shortcuts"),
            HelpEntry("Ctrl+G · Ctrl+L", "Talk / toggle voice listening", "Input & shortcuts"),
            HelpEntry("Shift+Enter", "Insert a new line in your prompt", "Input & shortcuts"),
            HelpEntry(
                "Ctrl+C · Ctrl+Q", "Copy selected text or quit / always quit", "Input & shortcuts"
            ),
            HelpEntry("Ctrl+Shift+Backspace", "Restore the default UI layout", "Input & shortcuts"),
        ]
    )
    return entries


class ReferenceScreen(ModalScreen[None]):
    """A shared frame that gives scrolling content the remaining screen space."""

    BINDINGS = [("escape", "close", "Close")]
    DEFAULT_CSS = """
    ReferenceScreen { align: center middle; }
    ReferenceScreen #reference-shell {
        width: 94%; max-width: 110; height: 92%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    ReferenceScreen .reference-title { height: 2; color: $primary; text-style: bold; }
    ReferenceScreen .reference-subtitle { height: auto; max-height: 3; color: $text-muted; }
    ReferenceScreen #reference-filters { height: auto; margin-top: 1; }
    ReferenceScreen #reference-search { width: 2fr; }
    ReferenceScreen #reference-category { width: 1fr; min-width: 20; }
    ReferenceScreen #reference-count { height: 1; color: $text-muted; margin: 1 0; }
    ReferenceScreen #reference-list { height: 1fr; min-height: 3; border: none; background: $background; }
    ReferenceScreen .reference-details {
        height: auto; max-height: 6; min-height: 2; padding: 0 1;
        border-left: thick $accent; background: $panel; margin-top: 1;
    }
    ReferenceScreen #reference-detail { height: auto; }
    ReferenceScreen #reference-actions { height: 3; align-horizontal: right; margin-top: 1; }
    ReferenceScreen #reference-actions Button { min-width: 10; margin-left: 1; }
    ReferenceScreen #evolution-totals { height: auto; color: $success; padding: 1 0; }
    ReferenceScreen.compact #reference-shell { width: 100%; height: 100%; padding: 0 1; }
    ReferenceScreen.compact #reference-filters { layout: vertical; margin: 0; }
    ReferenceScreen.compact #reference-search, ReferenceScreen.compact #reference-category { width: 100%; }
    ReferenceScreen.compact .reference-details { max-height: 4; padding: 0 1; margin: 0; }
    ReferenceScreen.compact #reference-count { margin: 0; }
    ReferenceScreen.compact #evolution-totals { padding: 0; }
    ReferenceScreen.compact #reference-actions { margin: 0; }
    """

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 70 or event.size.height < 32, "compact")

    def action_close(self) -> None:
        self.dismiss(None)

    def _palette(self):
        return cached_palette(self.app.get_theme(self.app.theme))


class HelpScreen(ReferenceScreen):
    def __init__(self, registry, plugins=(), panel_commands=(), skill_loader=None):
        super().__init__()
        self.registry = registry
        self.plugins = plugins
        self.panel_commands = panel_commands
        self.skill_loader = skill_loader
        self.entries = help_entries(registry, plugins, panel_commands)
        self.visible_entries: list[HelpEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="reference-shell"):
            yield Static("NOVA  /  COMMAND GUIDE", classes="reference-title")
            yield Static(
                "Find a command, explore its aliases, or browse keyboard shortcuts.",
                classes="reference-subtitle",
            )
            with Horizontal(id="reference-filters"):
                yield Input(
                    placeholder="Search commands, aliases, or descriptions…", id="reference-search"
                )
                yield Select(
                    [
                        (n, n)
                        for n in (
                            "All",
                            *_GROUPS,
                            "Other commands",
                            "Plugins",
                            "UI panels",
                            "Skills",
                            "Input & shortcuts",
                        )
                    ],
                    value="All",
                    allow_blank=False,
                    id="reference-category",
                )
            yield Static("", id="reference-count")
            yield OptionList(id="reference-list")
            with VerticalScroll(classes="reference-details"):
                yield Static("", id="reference-detail", markup=False)
            with Horizontal(id="reference-actions"):
                yield Button("Close", id="reference-close")

    def on_mount(self) -> None:
        self._reload()
        self.query_one(Input).focus()
        if self.skill_loader:
            self._load_skills()

    @work(exclusive=True)
    async def _load_skills(self) -> None:
        try:
            skills = await asyncio.to_thread(self.skill_loader)
        except Exception:  # noqa: BLE001 — keep built-in help available
            return
        self.entries = help_entries(self.registry, self.plugins, self.panel_commands, skills)
        self._reload()

    def on_input_changed(self) -> None:
        self._reload()

    def on_select_changed(self) -> None:
        self._reload()

    def _reload(self) -> None:
        if not self.is_mounted:
            return
        query = self.query_one(Input).value.strip().casefold()
        category = self.query_one(Select).value
        matches = [
            e
            for e in self.entries
            if (category == "All" or category == e.category)
            and (query or category != "All" or e.category != "Skills")
            and query in " ".join((e.command, e.description, e.category, *e.aliases)).casefold()
        ]
        self.visible_entries = matches[:200]
        palette = self._palette()
        options = []
        for entry in self.visible_entries:
            label = Text(entry.command, style=f"bold {palette.primary}")
            label.append(f"  ·  {entry.category}\n", style=palette.muted)
            label.append(entry.description, style=palette.text)
            options.append(Option(label))
        self.query_one(OptionList).clear_options().add_options(options)
        count = (
            f"{len(matches)} results"
            if len(matches) <= 200
            else f"Showing 200 of {len(matches)} results — narrow your search"
        )
        self.query_one("#reference-count", Static).update(count + "  ·  ↑↓ browse  ·  Esc close")
        self.query_one(OptionList).highlighted = 0 if options else None
        if not options:
            self.query_one("#reference-detail", Static).update(
                "No matching commands. Try another search or category."
            )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index >= len(self.visible_entries):
            return
        entry = self.visible_entries[event.option_index]
        detail = Text(entry.command + "\n", style="bold")
        detail.append(entry.description + "\n", style="none")
        if entry.aliases:
            detail.append("Aliases  " + "  ·  ".join(entry.aliases) + "\n")
        if entry.example:
            detail.append(entry.example)
        self.query_one("#reference-detail", Static).update(detail)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()


class EvolutionScreen(ReferenceScreen):
    def __init__(self):
        super().__init__()
        self.entries: list[dict[str, Any]] = []
        self.visible_entries: list[dict[str, Any]] = []
        self._loading = True
        self._load_error: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="reference-shell"):
            yield Static("NOVA  /  EVOLUTION", classes="reference-title")
            yield Static(
                "Skills gained and strengthened through completed work.",
                classes="reference-subtitle",
            )
            yield Static("Loading evolution history…", id="evolution-totals")
            with Horizontal(id="reference-filters"):
                yield Input(placeholder="Search a skill or task…", id="reference-search")
                yield Select(
                    [("All activity", "all"), ("Unlocked", "unlock"), ("Levelled up", "levelup")],
                    value="all",
                    allow_blank=False,
                    id="reference-category",
                )
            yield Static("", id="reference-count")
            yield OptionList(id="reference-list")
            with VerticalScroll(classes="reference-details"):
                yield Static("", id="reference-detail", markup=False)
            with Horizontal(id="reference-actions"):
                yield Button("Refresh", id="evolution-refresh", variant="primary")
                yield Button("Close", id="reference-close")

    def on_mount(self) -> None:
        self._load()

    @work(exclusive=True)
    async def _load(self) -> None:
        from novacode_cli.commands.evolution_handler import _load_evolution

        self.query_one("#evolution-refresh", Button).disabled = True
        self._loading = True
        self._load_error = None
        self.query_one("#evolution-totals", Static).update("Loading evolution history…")
        try:
            counters, self.entries = await _load_evolution(strict=True)
            self.query_one("#evolution-totals", Static).update(
                f"◈  {counters['unlocked']:,} skills unlocked    ↑  {counters['leveled']:,} level-ups"
            )
        except Exception as error:
            self._load_error = str(error)
            self.query_one("#evolution-totals", Static).update("History unavailable — try Refresh.")
        # Cancellation skips these paints: a dismissed screen has no children.
        self._loading = False
        self._reload()
        self.query_one("#evolution-refresh", Button).disabled = False

    def on_input_changed(self) -> None:
        self._reload()

    def on_select_changed(self) -> None:
        self._reload()

    def _reload(self) -> None:
        if not self.is_mounted:
            return
        query = self.query_one(Input).value.strip().casefold()
        kind = self.query_one(Select).value
        self.visible_entries = [
            e
            for e in self.entries
            if (kind == "all" or e.get("kind") == kind)
            and query in f"{e.get('skill', '')} {e.get('task_summary', '')}".casefold()
        ]
        palette = self._palette()
        options = []
        for entry in self.visible_entries:
            unlock = entry.get("kind") == "unlock"
            label = Text(
                "◈ " if unlock else "↑ ", style=palette.success if unlock else palette.accent
            )
            label.append(str(entry.get("skill", "Unknown skill")), style=f"bold {palette.text}")
            label.append(
                "\n" + ("Unlocked" if unlock else "Levelled up") + "  ·  " + self._when(entry),
                style=palette.muted,
            )
            options.append(Option(label))
        self.query_one(OptionList).clear_options().add_options(options)
        self.query_one("#reference-count", Static).update(
            f"{len(options)} recent events  ·  timestamps in UTC"
        )
        self.query_one(OptionList).highlighted = 0 if options else None
        if not options:
            self.query_one("#reference-detail", Static).update(
                "Loading evolution history…"
                if self._loading
                else f"Could not load evolution history: {self._load_error}"
                if self._load_error
                else "No matching activity."
                if self.entries
                else "Your evolution starts with completed work. Finish a complex task involving edits, tests, or subagents to unlock or strengthen a skill."
            )

    @staticmethod
    def _when(entry) -> str:
        try:
            ts = float(entry.get("ts", 0))
            if ts <= 0:
                return "Time unavailable"
            return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError, OverflowError, OSError):
            return "Time unavailable"

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index < len(self.visible_entries):
            entry = self.visible_entries[event.option_index]
            self.query_one("#reference-detail", Static).update(
                str(entry.get("task_summary") or "No task summary recorded for this evolution.")
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "evolution-refresh":
            self._load()
        else:
            self.action_close()
