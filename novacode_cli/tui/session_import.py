"""External session browser and Nova continuation controls."""
# ruff: noqa: ANN401, PLR2004 — framework app boundary and fixed command arities

from __future__ import annotations

import asyncio
import shlex
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from langchain_core.messages import HumanMessage
from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static

from novacode_cli.tui.widgets import OutputLog
from novacode_cli.session.adapters import ImportedSession, SessionInfo, adapters
from novacode_cli.session.imported_context import (
    MESSAGE_ID,
    ImportedContext,
    git_state,
    import_budget,
    transcript,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from novacode_cli.session.adapters import SessionAdapter


class TranscriptScreen(ModalScreen[None]):
    """View history without modifying the graph or session files."""

    BINDINGS: ClassVar = [("escape", "close", "Close")]
    DEFAULT_CSS = """
    TranscriptScreen #import-preview {
        width: 90%; height: 85%; padding: 1 2;
        background: $surface; border: round $accent;
    }
    TranscriptScreen OutputLog { height: 1fr; }
    """

    def __init__(self, session: ImportedSession) -> None:
        """Prepare the session browser or transcript preview."""
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        """Build the session controls."""
        with Vertical(id="import-preview"):
            yield Static(Text(f"{self.session.provider} · {self.session.session_id}", style="bold"))
            yield OutputLog(id="import-history", wrap=True, markup=False)
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        # Bound rendering separately; the normalized source remains complete.
        """Load the historical session content."""
        self.query_one(OutputLog).write(Text(transcript(self.session)[:200000]))
        if len(transcript(self.session)) > 200000:
            self.query_one(OutputLog).write(Text("Preview limited to 200,000 characters."))

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        """Handle viewing, importing, or closing."""
        self.dismiss(None)

    def action_close(self) -> None:
        """Close without importing."""
        self.dismiss(None)


class ExternalSessionsScreen(ModalScreen[SessionInfo | None]):
    """Discover, search, view, and explicitly select an external transcript."""

    BINDINGS: ClassVar = [("escape", "close", "Close")]
    DEFAULT_CSS = """
    ExternalSessionsScreen #external-browser {
        width: 90%; height: 80%; padding: 1 2;
        background: $surface; border: round $accent;
    }
    ExternalSessionsScreen OptionList { height: 1fr; }
    ExternalSessionsScreen #external-hint { height: auto; }
    """

    def __init__(self, registry: dict[str, Any]) -> None:
        """Prepare the session browser or transcript preview."""
        super().__init__()
        self.registry = registry
        self.rows: list[SessionInfo] = []
        self.filtered: list[SessionInfo] = []

    def compose(self) -> ComposeResult:
        """Build the session controls."""
        with Vertical(id="external-browser"):
            yield Static(Text("External sessions", style="bold"))
            yield Input(
                placeholder="Search project, task, provider, or session ID", id="external-search"
            )
            yield OptionList(id="external-sessions")
            yield Static("Discovering local transcripts…", id="external-hint")
            with Horizontal():
                yield Button("View", id="external-view")
                yield Button("Import", id="external-import", variant="primary")
                yield Button("Close", id="external-close")

    @work(exclusive=True, exit_on_error=False)
    async def on_mount(self) -> None:
        """Load the historical session content."""
        results = await asyncio.gather(
            *(
                asyncio.to_thread(adapter.discover)
                for adapter in self.registry.values()
                if adapter.provider != "nova"
            ),
            return_exceptions=True,
        )
        errors = sum(isinstance(result, BaseException) for result in results)
        self.rows = sorted(
            [row for result in results if isinstance(result, list) for row in result],
            key=lambda row: row.updated_at,
            reverse=True,
        )
        self.filter_rows(self.query_one(Input).value)
        if errors:
            self.query_one("#external-hint", Static).update(
                "Some providers could not be read. Explicit file imports remain available."
            )

    def filter_rows(self, query: str) -> None:
        """Search discovery metadata without importing any transcript."""
        self.filtered = [
            row
            for row in self.rows
            if query.lower() in f"{row.provider} {row.session_id} {row.cwd} {row.title}".lower()
        ]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            [
                Text(
                    f"{row.provider.title():8} {row.session_id[:12]} · {row.title} · "
                    f"{row.message_count} messages"
                )
                for row in self.filtered
            ]
        )
        if self.filtered:
            options.highlighted = 0
        self.query_one("#external-hint", Static).update(
            "View reads history. Import loads compact context into this Nova conversation."
            if self.filtered
            else "No matching sessions. Use /import <provider> <file> to load an export."
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the discovered sessions."""
        self.filter_rows(event.value)

    @work(exclusive=True, exit_on_error=False)
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle viewing, importing, or closing."""
        if event.button.id == "external-close":
            self.dismiss(None)
            return
        index = self.query_one(OptionList).highlighted
        if index is None or not 0 <= index < len(self.filtered):
            return
        row = self.filtered[index]
        if event.button.id == "external-import":
            self.dismiss(row)
        elif event.button.id == "external-view":
            try:
                session = await asyncio.to_thread(self.registry[row.provider].load, row.path)
                await self.app.push_screen_wait(TranscriptScreen(session))
            except (ValueError, OSError) as exc:
                self.query_one("#external-hint", Static).update(Text(str(exc)))

    def action_close(self) -> None:
        """Close without importing."""
        self.dismiss(None)


def parse_import(text: str) -> tuple[str, str, str]:
    """Parse a slash import; quoted paths retain Windows backslashes."""
    parts = shlex.split(text, posix=False)
    parts = [part.strip("\"'") for part in parts]
    if len(parts) < 2:
        message = (
            'Use /import codex|claude|nova <session ID or "file path"> '
            "[--mode compact|full|relevant], or --last."
        )
        raise ValueError(message)
    mode = "compact"
    if "--mode" in parts:
        index = parts.index("--mode")
        if index + 1 >= len(parts):
            message = "--mode requires full, compact, or relevant."
            raise ValueError(message)
        mode = parts[index + 1]
        del parts[index : index + 2]
    selector = parts[2] if len(parts) == 3 else ""
    if not selector:
        message = "Supply a session ID, file path, or --last."
        raise ValueError(message)
    return parts[1].lower(), selector, mode


def load_import(
    provider: str,
    selector: str,
    mode: str = "compact",
    *,
    registry: dict[str, SessionAdapter] | None = None,
) -> ImportedContext:
    """Resolve a provider and explicit selector; latest is scoped to that provider."""
    registry = registry or adapters()
    if provider not in registry:
        message = f"Unknown session provider '{provider}'. Available: {', '.join(registry)}"
        raise ValueError(message)
    adapter = registry[provider]
    if selector in {"--last", "--latest"}:
        rows = adapter.discover()
        if not rows:
            message = f"No local {provider} sessions were found."
            raise ValueError(message)
        selector = max(rows, key=lambda row: row.updated_at).path
    context = ImportedContext(adapter.load(selector), mode, git_at_import=git_state(Path.cwd()))
    context.build()
    return context


def context_budget(app: Any) -> int:
    """Leave room for Nova's prompt, tools, ongoing chat, and the next response."""
    tracker = app.token_tracker
    window = getattr(tracker, "context_window_size", 0)
    used = getattr(tracker, "current_context", 0)
    if isinstance(window, (int, float)) and window > 0:
        return import_budget(int(window), int(used))
    return 12000


async def install_context(app: Any, context: ImportedContext) -> None:
    """Replace only the imported reference message; keep Nova history intact."""
    config = {"configurable": {"thread_id": app.session_state.thread_id}}
    budget = context_budget(app)
    content = context.build(budget)
    # LangGraph's message reducer replaces a matching ID. Combining remove and
    # append for the same ID would delete the replacement at the end of reduction.
    updates = [HumanMessage(content=content, id=MESSAGE_ID)]
    await app.agent.aupdate_state(config, values={"messages": updates}, as_node="model")
    if app.session_manager:
        await asyncio.to_thread(
            context.save, app.session_manager.sessions_dir, app.session_state.session_id
        )
    await app._save_session()
    app._log(Text(context.describe(budget), style="cyan"))
    await app._update_context_breakdown()
    app._refresh_status()


async def dispatch_import_command(app: Any, text: str) -> None:  # noqa: PLR0912 — four command routes
    """Run session browse, import, context switching, or cross-agent comparison."""
    parts = text.split(maxsplit=3)
    command = parts[0].lower()
    try:
        if command == "/sessions":
            registry = await asyncio.to_thread(adapters)
            if len(parts) == 1:
                from novacode_cli.tui.screens import PickScreen

                choice = await app.push_screen_wait(
                    PickScreen(
                        "Sessions", ["Nova saved sessions", "External sessions — Claude / Codex"]
                    )
                )
                if choice == 0:
                    await app._run_sessions()
                    return
                if choice != 1:
                    return
            elif parts[1].lower() != "external":
                await app._run_sessions()
                return
            row = await app.push_screen_wait(ExternalSessionsScreen(registry))
            if row is not None:
                context = await asyncio.to_thread(
                    load_import, row.provider, row.path, registry=registry
                )
                await install_context(app, context)
        elif command == "/import":
            provider, selector, mode = parse_import(text)
            context = await asyncio.to_thread(load_import, provider, selector, mode)
            await install_context(app, context)
        elif command == "/context":
            if len(parts) == 1 or parts[1].lower() != "imported":
                app._run_token_view()
                return
            if app.session_manager is None:
                message = "Imported context requires session storage."
                raise ValueError(message)  # noqa: TRY301 — report command errors locally
            loaded_context = await asyncio.to_thread(
                ImportedContext.load, app.session_manager.sessions_dir, app.session_state.session_id
            )
            if loaded_context is None:
                message = "This Nova session has no imported context. Use /import first."
                raise ValueError(message)  # noqa: TRY301 — report command errors locally
            if len(parts) == 2:
                app._log(Text(loaded_context.describe(context_budget(app)), style="cyan"))
                return
            context = replace(
                loaded_context, mode=parts[2].lower(), query=parts[3] if len(parts) > 3 else ""
            )
            await install_context(app, context)
        elif command == "/compare":
            if len(parts) != 3 or any(":" not in part for part in parts[1:]):
                message = "Use /compare claude:<id> codex:<id>."
                raise ValueError(message)  # noqa: TRY301 — report command errors locally
            contexts = await asyncio.gather(
                *(asyncio.to_thread(load_import, *selector.split(":", 1)) for selector in parts[1:])
            )
            # Comparison remains an ordinary new Nova request. No source tools
            # or source agent runtimes are resumed.
            prompt = (
                "Compare these historical sessions: decisions, approaches, files touched, "
                "test results, and unresolved problems. Cite transcript evidence and "
                "distinguish it from current workspace state.\n\n"
            )
            prompt += "\n\n".join(context.build(context_budget(app) // 2) for context in contexts)
            await app._stream_prompt(prompt)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        app._log(Text(f"Session import: {exc}", style="yellow"))
