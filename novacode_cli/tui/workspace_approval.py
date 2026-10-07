"""A lightweight workspace-access screen shown before the chat app starts."""

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

COMPACT_ROWS = 28
NARROW_COLUMNS = 50


class WorkspaceApprovalApp(App[str | None]):
    """Return an explicit scope choice; closing the screen grants nothing."""

    CSS = """
    Screen { align: center middle; background: $background; }
    #workspace-card {
        width: 92%; max-width: 88; height: 90%; max-height: 42;
        border: round $accent; background: $surface; padding: 0 2;
    }
    #workspace-header {
        height: 2; padding-top: 1; color: $text-muted; text-style: bold;
    }
    #workspace-content { height: 1fr; scrollbar-gutter: stable; }
    #workspace-art {
        height: auto; content-align: center middle;
        color: $primary; text-style: bold; margin: 1 0;
    }
    #workspace-title { height: auto; text-style: bold; margin-bottom: 1; }
    #workspace-path {
        height: auto; padding: 1 2; border-left: thick $primary;
        background: $boost; margin-bottom: 1;
    }
    #workspace-description, .scope-description {
        height: auto; color: $text-muted; margin-bottom: 1;
    }
    #workspace-choices { height: auto; }
    #workspace-choices Button { width: 1fr; height: 3; }
    .scope-description { padding: 0 1; }
    #workspace-footer { height: 4; align: right middle; padding-top: 1; }
    #workspace-hint { width: 1fr; height: auto; color: $text-muted; }
    #workspace-deny { min-width: 12; margin-left: 1; }
    Screen.compact #workspace-header { height: 1; padding-top: 0; }
    Screen.compact #workspace-footer { height: 3; padding-top: 0; }
    Screen.compact #workspace-art { margin: 0; }
    Screen.compact #workspace-title { margin-bottom: 0; }
    Screen.compact #workspace-path { padding: 0 1; }
    Screen.compact .scope-description { display: none; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "approve_tree", "Folder + subfolders", priority=True),
        Binding("o", "approve_only", "Folder only", priority=True),
        Binding("n,escape,ctrl+c", "deny", "Exit", priority=True),
        Binding("down", "focus_next", show=False),
        Binding("up", "focus_previous", show=False),
    ]

    def __init__(self, path: Path) -> None:
        """Prepare the folder and use the user's saved Nova theme."""
        super().__init__()
        self.workspace = path.resolve()
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.tui.widgets import DEFAULT_THEME, NOVA_MATRIX, NOVA_TOKYO_NIGHT

        self.register_theme(NOVA_TOKYO_NIGHT)
        self.register_theme(NOVA_MATRIX)
        saved = NovaConfig().get("theme")
        self.theme = (
            saved if isinstance(saved, str) and saved in self.available_themes else DEFAULT_THEME
        )

    def compose(self) -> ComposeResult:
        """Build the branded card and its explicit approval choices."""
        with Vertical(id="workspace-card"):
            yield Static("NOVACODE  /  WORKSPACE ACCESS", id="workspace-header")
            with VerticalScroll(id="workspace-content"):
                yield Static("", id="workspace-art")
                yield Static("Allow Nova to work in this folder?", id="workspace-title")
                yield Static(Text(str(self.workspace)), id="workspace-path")
                yield Static(
                    "Nova can read and edit files and run commands from this folder. "
                    "Tool approvals still follow your settings.",
                    id="workspace-description",
                )
                with Vertical(id="workspace-choices"):
                    yield Button("Y · Folder + subfolders", id="workspace-tree", variant="primary")
                    yield Static(
                        "Remember access here and in every subfolder.", classes="scope-description"
                    )
                    yield Button("O · This folder only", id="workspace-only")
                    yield Static(
                        "Ask again when Nova starts in an unapproved subfolder.",
                        classes="scope-description",
                    )
                    yield Static(
                        "Manage saved access later with nova paths.", classes="scope-description"
                    )
            with Horizontal(id="workspace-footer"):
                yield Static("Y / O approve\nEsc exits · Tab moves", id="workspace-hint")
                yield Button("Exit Nova", id="workspace-deny")

    def on_mount(self) -> None:
        """Show the artwork and focus the safe exit choice."""
        self._refresh_art()
        self.query_one("#workspace-deny", Button).focus(scroll_visible=False)

    def on_resize(self, event: events.Resize) -> None:
        """Reflow using the new terminal dimensions."""
        if self.is_mounted:
            self._refresh_art(event.size.width, event.size.height)

    def _refresh_art(self, columns: int | None = None, rows: int | None = None) -> None:
        from novacode_cli.config.config import get_responsive_ascii

        columns = self.size.width if columns is None else columns
        rows = self.size.height if rows is None else rows
        self.screen.set_class(rows < COMPACT_ROWS, "compact")
        self.query_one("#workspace-header", Static).update(
            "NOVA / WORKSPACE" if rows < COMPACT_ROWS else "NOVACODE  /  WORKSPACE ACCESS"
        )
        self.query_one("#workspace-description", Static).update(
            "Read & edit files; run commands. Tool approvals apply."
            if rows < COMPACT_ROWS
            else "Nova can read and edit files and run commands from this folder. "
            "Tool approvals still follow your settings."
        )
        narrow = columns < NARROW_COLUMNS
        self.query_one("#workspace-tree", Button).label = (
            "Y + subfolders" if narrow else "Y · Folder + subfolders"
        )
        self.query_one("#workspace-only", Button).label = (
            "O · Only here" if narrow else "O · This folder only"
        )
        self.query_one("#workspace-hint", Static).update(
            "Esc exits" if narrow else "Y / O save access\nEsc exits · Tab moves"
        )
        width = max(1, min(88, int(columns * 0.92)) - 8)
        art = get_responsive_ascii(width=width).strip("\n")
        if rows < COMPACT_ROWS:
            art = "N O V A"
        self.query_one("#workspace-art", Static).update(Text(art))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return only the scope selected by a button."""
        choices = {"workspace-tree": "recursive", "workspace-only": "only"}
        self.exit(choices.get(event.button.id))

    def action_approve_tree(self) -> None:
        """Approve the folder and its subfolders."""
        self.exit("recursive")

    def action_approve_only(self) -> None:
        """Approve this folder without extending startup consent to children."""
        self.exit("only")

    def action_deny(self) -> None:
        """Close the screen without granting access."""
        self.exit(None)
