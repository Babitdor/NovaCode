"""Release-aware notices in the transcript and a scrollable changelog page."""

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Link, Static

from novacode_cli.updates import UpdateStatus


class ChangelogScreen(ModalScreen[None]):
    """Show fetched release notes and an official link to the full changelog."""

    DEFAULT_CSS = """
    ChangelogScreen #modal-box { width: 90%; max-width: 100; height: 90%; }
    ChangelogScreen #changelog-content { height: 1fr; min-height: 3; }
    ChangelogScreen #changelog-body { height: auto; }
    ChangelogScreen #changelog-link { height: auto; margin: 1 0; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, status: UpdateStatus) -> None:
        super().__init__()
        self._status = status

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(
                Text(self._status.release_title or "NovaCode changelog", style="bold"),
                id="modal-title",
            )
            with VerticalScroll(id="changelog-content"):
                notes = (
                    self._status.release_notes
                    or "Release notes could not be loaded. Open the changelog below."
                )
                yield Static(Markdown(notes), id="changelog-body")
                yield Link(
                    "Open full changelog on GitHub ↗",
                    url=self._status.changelog_url,
                    id="changelog-link",
                )
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            event.stop()
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class UpdateNotice(Vertical):
    """A persistent update notice with a preview and View more action."""

    DEFAULT_CSS = """
    UpdateNotice { height: auto; margin: 1 0; padding: 1 2; border-left: thick $warning; }
    UpdateNotice Static { height: auto; }
    UpdateNotice Button { margin-top: 1; }
    """

    def __init__(self, status: UpdateStatus) -> None:
        super().__init__()
        self._status = status

    def compose(self) -> ComposeResult:
        release = self._status.release_title or f"NovaCode update ({self._status.latest[:12]})"
        yield Static(Text(f"Update available · {release}", style="bold yellow"))
        changes = [
            line[:160] for line in self._status.release_notes.splitlines() if line.startswith("- ")
        ][:4]
        if changes:
            yield Static(Text("\n".join(changes)))
        yield Static(Text("Exit Nova, then run: nova update", style="dim"))
        yield Button("View more · changelog", classes="update-view-more")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("update-view-more"):
            event.stop()
            self.app.push_screen(ChangelogScreen(self._status))
