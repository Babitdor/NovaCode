"""Bounded, read-only presentation of normalized historical conversations."""

from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Static

from novacode_cli.session.adapters import HarnessMessage, ImportedSession
from novacode_cli.tui.code_mode_panel import CODE_MODE_TOOLS, CodeModePanel
from novacode_cli.tui.diff_preview import DiffPreview
from novacode_cli.tui.widgets import ChatMessage, OutputLog

_TEXT_LIMIT = 16000


def saved_tool_card(message: HarnessMessage) -> CodeModePanel:
    """Rehydrate the live tool widget using only the saved record."""
    name = message.tool_name or "tool"
    args = message.metadata.get("arguments")
    if isinstance(args, dict):
        card = CodeModePanel.for_call(name, args, live=False)
    else:
        display = message.metadata.get("display")
        if not isinstance(display, str) and name not in CODE_MODE_TOOLS:
            display = name
        source = message.metadata.get("source", "(arguments unavailable)")
        card = CodeModePanel.from_source(name, source, display, live=False)
    card.restore_result(
        message.content, is_error=bool(message.metadata.get("is_error")),
        preview=message.metadata.get("preview"),
    )
    if message.metadata.get("interrupted"):
        card.heading.update(Text.assemble(("Interrupted ", "bold"), card.display_str or name))
    if isinstance(card.output, DiffPreview) and (diff := message.metadata.get("diff")):
        card.output.restore(diff)
    if "collapsed" in message.metadata and not isinstance(card.output, OutputLog):
        card.collapsed = message.metadata["collapsed"]
    return card


def history_card(
    message: HarnessMessage, provider: str = "nova", *, native_scrollback: bool = False
) -> ChatMessage | Collapsible:
    """Create a read-only historical message or tool card."""
    if native_scrollback and (message.tool_name or message.role == "tool"):
        return saved_tool_card(message)
    content = message.content[:_TEXT_LIMIT]
    if len(message.content) > _TEXT_LIMIT:
        content += "\n\n[Display excerpt; complete text is retained in the source session.]"
    if message.tool_name or message.role == "tool":
        kind = "Call" if message.metadata.get("kind") == "tool_call" else "Result"
        status = " · error" if message.metadata.get("is_error") else ""
        log = OutputLog(wrap=True, markup=False, max_lines=17000, max_chars=20000)
        log.write(Text(content))
        return Collapsible(
            log,
            title=escape(f"{kind} · {message.tool_name or 'tool'}{status} · historical"),
            collapsed=True,
        )
    else:
        label = (
            "User"
            if message.role == "user"
            else (provider.title() if message.role == "assistant" else "System")
        )
        label = str(message.metadata.get("label") or label)
        header = label + (f" · {message.timestamp}" if message.timestamp else "")
        card = ChatMessage(Text(header), "user" if message.role == "user" else "nova")
        card.update_body(Markdown(content))
        return card


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

    def __init__(
        self, session: ImportedSession, *, latest: bool = False, heading: str = "Imported history"
    ) -> None:
        super().__init__()
        self.session = session
        self.heading = heading
        self.page = (max(0, len(session.messages) - 1) // self.PAGE_SIZE) if latest else 0

    def compose(self):
        yield Static(
            Text(
                f"{self.heading} · {self.session.provider.title()} · {self.session.session_id}\n"
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
        widgets = [history_card(message, self.session.provider) for message in messages]
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
