"""Bounded, read-only presentation of normalized historical conversations."""

from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Static

from novacode_cli.session.adapters import ImportedSession
from novacode_cli.tui.widgets import ChatMessage, OutputLog


class SessionTranscript(Vertical):
    """Page through history without turning historical tools into live tasks."""

    DEFAULT_CSS = """
    SessionTranscript { height: auto; }
    SessionTranscript > .history-heading { height: auto; }
    SessionTranscript > .history-page { height: auto; }
    SessionTranscript > Horizontal { height: 3; }
    SessionTranscript Button { min-width: 10; width: auto; }
    SessionTranscript Collapsible { height: auto; }
    """
    PAGE_SIZE = 20
    TEXT_LIMIT = 16000

    def __init__(self, session: ImportedSession, *, latest: bool = False) -> None:
        super().__init__()
        self.session = session
        self.page = (max(0, len(session.messages) - 1) // self.PAGE_SIZE) if latest else 0

    def compose(self):
        yield Static(
            Text(
                f"Imported history · {self.session.provider.title()} · {self.session.session_id}\n"
                f"Workspace: {self.session.cwd or 'unknown'}",
                style="bold",
            ),
            classes="history-heading",
        )
        yield Vertical(classes="history-page")
        with Horizontal():
            yield Button("Previous", classes="history-prev")
            yield Button("Next", classes="history-next")
            yield Static(classes="history-position")

    async def on_mount(self) -> None:
        await self.render_page()

    def on_unmount(self) -> None:
        """Release the source even when Textual retains removed node metadata."""
        self.session = ImportedSession("", "", None, [])

    async def render_page(self) -> None:
        body = self.query_one(".history-page", Vertical)
        await body.remove_children()
        start = self.page * self.PAGE_SIZE
        messages = self.session.messages[start : start + self.PAGE_SIZE]
        widgets = []
        for message in messages:
            content = message.content[: self.TEXT_LIMIT]
            if len(message.content) > self.TEXT_LIMIT:
                content += "\n\n[Display excerpt; complete text is retained in the source session.]"
            if message.tool_name or message.role == "tool":
                kind = "Call" if message.metadata.get("kind") == "tool_call" else "Result"
                status = " · error" if message.metadata.get("is_error") else ""
                log = OutputLog(wrap=True, markup=False, max_lines=17000, max_chars=20000)
                log.write(Text(content))
                widgets.append(
                    Collapsible(
                        log,
                        title=escape(
                            f"{kind} · {message.tool_name or 'tool'}{status} · historical"
                        ),
                        collapsed=True,
                    )
                )
            else:
                label = (
                    "User"
                    if message.role == "user"
                    else (
                        self.session.provider.title() if message.role == "assistant" else "System"
                    )
                )
                header = label + (f" · {message.timestamp}" if message.timestamp else "")
                card = ChatMessage(Text(header), "user" if message.role == "user" else "nova")
                card.update_body(Markdown(content))
                widgets.append(card)
        if widgets:
            await body.mount(*widgets)
        self.query_one(".history-prev", Button).disabled = self.page == 0
        self.query_one(".history-next", Button).disabled = start + len(messages) >= len(
            self.session.messages
        )
        self.query_one(".history-position", Static).update(
            f" {start + 1 if messages else 0}–{start + len(messages)}"
            f" / {len(self.session.messages)} messages"
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("history-prev") or event.button.has_class("history-next"):
            event.stop()
            self.page += -1 if event.button.has_class("history-prev") else 1
            await self.render_page()


async def show_imported_history(app, session: ImportedSession) -> None:
    """Replace the display when a new external reference is installed."""
    if not hasattr(app, "_transcript"):
        return
    target = app._transcript()
    for previous in target.query(SessionTranscript):
        await previous.remove()
    await target.mount(SessionTranscript(session, latest=True))
