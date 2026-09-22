"""Modal screens for the Nova TUI.

Extracted verbatim from :mod:`novacode_cli.tui.app` (which re-exports these
names for backward compatibility). May import from widgets.py; must not
import from app.py — screens reach the running app via ``self.app``.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    OptionList,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option

from novacode_cli.tui.animations import animate_modal_screen
from novacode_cli.tui.widgets import DEFAULT_THEME


class ConfirmModal(ModalScreen[bool]):
    """Simple yes/no confirmation (e.g. delete actions)."""

    BINDINGS = [
        ("y", "yes", "Yes"),
        ("n", "no", "No"),
        ("escape", "no", "No"),
    ]

    def __init__(self, title: str, body: Any) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style="bold"), id="modal-title")
            yield Static(self._body, id="modal-body")
            with Horizontal(id="modal-buttons"):
                yield Button("Yes (y)", id="yes", variant="error")
                yield Button("No (n)", id="no")

    def on_mount(self) -> None:
        animate_modal_screen(self)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ApprovalModal(ModalScreen[str]):
    """Rich approval dialog. Returns 'approve', 'auto', or 'reject'.

    Shows the action details and a keyboard-navigable choice list (arrows +
    Enter, or y/a/n quick keys, Esc to reject)."""

    BINDINGS = [
        ("y", "approve", "Approve"),
        ("s", "session", "Session"),
        ("l", "always", "Always"),
        ("a", "auto", "Auto-approve"),
        ("n", "reject", "Reject"),
        ("escape", "reject", "Reject"),
    ]

    def __init__(self, title: str, body: Any, *, allow_auto: bool = True) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._allow_auto = allow_auto

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text(f">>> {self._title} <<<", style="bold yellow"), id="modal-title")
            # Bound + scroll the body so a long plan can't push the choices
            # off-screen — the options must always stay visible.
            with VerticalScroll(id="modal-body-scroll"):
                yield Static(self._body, id="modal-body")
            yield OptionList(id="choices")
            yield Static(
                Text(
                    "↑/↓ navigate · Enter select · y/a/n quick keys · Esc reject",
                    style="dim",
                ),
                id="modal-hint",
            )

    def on_mount(self) -> None:
        animate_modal_screen(self)
        ol = self.query_one("#choices", OptionList)
        ol.add_option(Option("Approve (y)"))
        ol.add_option(Option("Allow for session (s)"))
        ol.add_option(Option("Always allow… (l)"))
        if self._allow_auto:
            ol.add_option(Option("Auto-approve for this thread (a)"))
        ol.add_option(Option("Reject (n)"))
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        label = str(event.option.prompt)
        if label.startswith("Approve"):
            self.dismiss("approve")
        elif label.startswith("Allow for session"):
            self.dismiss("session")
        elif label.startswith("Always"):
            self.dismiss("always")
        elif label.startswith("Auto"):
            self.dismiss("auto")
        else:
            self.dismiss("reject")

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_session(self) -> None:
        self.dismiss("session")

    def action_always(self) -> None:
        self.dismiss("always")

    def action_auto(self) -> None:
        self.dismiss("auto" if self._allow_auto else "approve")

    def action_reject(self) -> None:
        self.dismiss("reject")


class PlanApprovalModal(ModalScreen[str]):
    """Plan-mode approval dialog. Returns 'auto', 'manual', or 'refine'.

    Distinct from the tool ``ApprovalModal``: a plan has no session/always-allow
    rules. The three choices mirror the rich-console flow — auto-approve edits,
    approve each edit, or keep refining the plan. Esc maps to 'refine' so the
    safe default never throws the plan away."""

    BINDINGS = [
        ("a", "auto", "Auto-approve edits"),
        ("m", "manual", "Manual edits"),
        ("r", "refine", "Refine"),
        ("escape", "refine", "Refine"),
    ]

    def __init__(self, title: str, body: Any) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text(f">>> {self._title} <<<", style="bold yellow"), id="modal-title")
            # Bound + scroll the body so a long plan can't push the choices
            # off-screen — the options must always stay visible.
            with VerticalScroll(id="modal-body-scroll"):
                yield Static(self._body, id="modal-body")
            yield OptionList(id="choices")
            yield Static(
                Text(
                    "↑/↓ navigate · Enter select · a/m/r quick keys · Esc refine",
                    style="dim",
                ),
                id="modal-hint",
            )

    def on_mount(self) -> None:
        animate_modal_screen(self)
        ol = self.query_one("#choices", OptionList)
        ol.add_option(Option("Auto-approve edits — run the plan, auto-approve each edit (a)"))
        ol.add_option(Option("Manual edits — run the plan, approve each edit (m)"))
        ol.add_option(Option("Refine — keep planning, describe the changes (r)"))
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        label = str(event.option.prompt)
        if label.startswith("Auto-approve"):
            self.dismiss("auto")
        elif label.startswith("Manual"):
            self.dismiss("manual")
        else:
            self.dismiss("refine")

    def action_auto(self) -> None:
        self.dismiss("auto")

    def action_manual(self) -> None:
        self.dismiss("manual")

    def action_refine(self) -> None:
        self.dismiss("refine")


class RememberRuleModal(ModalScreen[dict | None]):
    """Confirm an 'always allow' rule: editable value + project/global target.

    Dismisses with ``{"value": <str>, "target": "project"|"global"}`` or ``None``
    if cancelled.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, rule: Any) -> None:
        super().__init__()
        self._rule = rule

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text(">>> Always allow <<<", style="bold yellow"), id="modal-title")
            yield Static(
                Text(f"{self._rule.human}\n({self._rule.category} rule)", style="dim"),
                id="modal-body",
            )
            yield Input(value=self._rule.value, id="rule-value")
            with Horizontal(id="remember-buttons"):
                yield Button("Project", id="btn-project", variant="primary")
                yield Button("Global", id="btn-global")
                yield Button("Cancel", id="btn-cancel", variant="error")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self.query_one("#rule-value", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = self.query_one("#rule-value", Input).value.strip() or self._rule.value
        if event.button.id == "btn-project":
            self.dismiss({"value": value, "target": "project"})
        elif event.button.id == "btn-global":
            self.dismiss({"value": value, "target": "global"})
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class QuestionModal(ModalScreen[dict]):
    """Answer an ``ask_question`` interrupt (open-ended or option select)."""

    def __init__(self, question_request: dict) -> None:
        super().__init__()
        self._q = question_request or {}

    def compose(self) -> ComposeResult:
        prompt = self._q.get("question") or self._q.get("prompt") or "The agent has a question:"
        opts = self._q.get("options") or []
        with Vertical(id="modal-box"):
            yield Static(Text(str(prompt), style="bold"), id="modal-title")
            if opts:
                # Use Text (not a raw markup string) so an option containing
                # '[' can't be parsed as markup and crash the render.
                lines = "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(opts))
                yield Static(Text(lines), id="modal-body")
            yield Input(placeholder="Type your answer (or option number)…", id="answer")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        from novacode_cli.ui.question_prompt import QuestionResponse

        text = event.value.strip()
        opts = self._q.get("options") or []
        selected = None
        answer = text
        if text.isdigit() and opts:
            idx = int(text) - 1
            if 0 <= idx < len(opts):
                selected = idx
                answer = opts[idx]
        self.dismiss({"response": QuestionResponse(answer=answer, selected_index=selected)})


class ModelScreen(ModalScreen[dict | None]):
    """Native ``/model`` screen: pick a provider, optionally enter an API key,
    and choose (or free-type) a model. Returns the selection; the app performs
    the actual switch."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    #: Providers that accept a custom OpenAI-compatible endpoint. OpenRouter and
    #: OpenCode are also OpenAI-compatible but pin their own base URL, so
    #: offering a box there would only let the user break the gateway.
    _ENDPOINT_PROVIDERS = frozenset({"openai"})

    def __init__(
        self,
        current_provider: str | None,
        configured: set[str],
        current_base_url: str | None = None,
    ) -> None:
        super().__init__()
        self._current = current_provider
        self._configured = configured
        self._current_base_url = current_base_url or ""

    def compose(self) -> ComposeResult:
        from novacode_cli.config.model_manager import MODEL_PRESETS

        options = []
        for pid, preset in MODEL_PRESETS.items():
            mark = "" if pid in self._configured else "  (needs key)"
            options.append((f"{preset['name']}{mark}", pid))

        default_value = self._current if self._current in MODEL_PRESETS else Select.BLANK
        with Vertical(id="modal-box"):
            yield Static(Text("Switch model", style="bold"), id="modal-title")
            yield Select(options, value=default_value, id="provider", allow_blank=True)
            yield Static("", id="modelinfo")
            # For Ollama: a live list of installed models (from `ollama list`).
            # Hidden for other providers (shown via _refresh_info).
            yield OptionList(id="modellist")
            yield Input(placeholder="API key (blank = use saved)", password=True, id="apikey")
            # OpenAI-compatible endpoint override. Shown only for providers that
            # accept one (see _ENDPOINT_PROVIDERS) — the gateways pin their own
            # base URL, and Anthropic/Google/Ollama don't take one at all.
            yield Input(
                placeholder="Endpoint URL (blank = api.openai.com)", id="baseurl"
            )
            yield Input(placeholder="Model (blank = default, or type any slug)", id="model")
            with Horizontal(id="modal-buttons"):
                yield Button("Switch", id="switch", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        # List is shown only for Ollama; hide until a provider is chosen.
        self.query_one("#modellist", OptionList).display = False
        # Endpoint box likewise: hidden until a provider that accepts one is
        # selected, so the modal stays short for everyone else.
        self.query_one("#baseurl", Input).display = False
        if self._current:
            self._refresh_info(self._current)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider" and event.value is not Select.BLANK:
            self._refresh_info(str(event.value))

    def _refresh_info(self, pid: str) -> None:
        from novacode_cli.config.model_manager import MODEL_PRESETS

        preset = MODEL_PRESETS.get(pid)
        if not preset:
            return
        info = Text()
        info.append(f"default: {preset['default_model']}\n", style="dim")
        model_list = self.query_one("#modellist", OptionList)

        if pid == "ollama":
            # Populate the list from `ollama list` (off the event loop).
            info.append("loading installed models (ollama list)…", style="dim")
            model_list.display = True
            self._load_ollama_models()
        else:
            models = preset.get("models", [])
            if models:
                info.append("suggestions: " + ", ".join(models[:6]), style="dim")
            model_list.display = False
            model_list.clear_options()

        base_input = self.query_one("#baseurl", Input)
        show_endpoint = pid in self._ENDPOINT_PROVIDERS
        base_input.display = show_endpoint
        if show_endpoint:
            # Prefill the saved endpoint so it is visible and editable rather
            # than silently still in effect.
            if not base_input.value:
                base_input.value = self._current_base_url
        else:
            base_input.value = ""

        self.query_one("#modelinfo", Static).update(info)
        self.query_one("#model", Input).placeholder = f"Model (blank = {preset['default_model']})"

    @work(exclusive=True)
    async def _load_ollama_models(self) -> None:
        """Populate the Ollama model list from `ollama list` without blocking."""
        import asyncio

        from novacode_cli.config.model_manager import get_ollama_models

        try:
            models = await asyncio.to_thread(get_ollama_models)
        except Exception:  # noqa: BLE001
            models = []

        try:
            model_list = self.query_one("#modellist", OptionList)
        except Exception:  # noqa: BLE001
            return  # screen dismissed before load finished
        model_list.clear_options()

        if not models:
            model_list.add_option(
                Option(
                    "No Ollama models found — is `ollama` installed/running?",
                    id="__none__",
                )
            )
            return

        for name in models:
            model_list.add_option(Option(name, id=name))

        info = Text()
        info.append(
            f"{len(models)} installed model(s) — select one or type a slug below\n",
            style="dim",
        )
        try:
            self.query_one("#modelinfo", Static).update(info)
        except Exception:  # noqa: BLE001
            pass

    def on_option_list_option_selected(self, event: "OptionList.OptionSelected") -> None:
        # Only the Ollama model list lives on this screen.
        if event.option_list.id != "modellist":
            return
        chosen = event.option.id
        if chosen and chosen != "__none__":
            self.query_one("#model", Input).value = chosen

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the API-key / model field applies the switch (same as the
        # Switch button) — a keyboard path so the user is never blocked when the
        # button is scrolled off a short terminal (e.g. behind the Ollama list).
        self._submit()

    def _submit(self) -> None:
        provider = self.query_one("#provider", Select).value
        if provider is Select.BLANK:
            self.dismiss(None)
            return
        pid = str(provider)
        # Only report an endpoint for providers that take one, so switching away
        # from OpenAI can't carry a stale URL onto a gateway.
        base_url = (
            self.query_one("#baseurl", Input).value.strip()
            if pid in self._ENDPOINT_PROVIDERS
            else ""
        )
        self.dismiss(
            {
                "provider": pid,
                "model": self.query_one("#model", Input).value.strip(),
                "api_key": self.query_one("#apikey", Input).value.strip(),
                "base_url": base_url,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionsScreen(ModalScreen[None]):
    """List and delete saved sessions (resume is via ``nova --continue <id>``)."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, session_manager, current_id: str | None) -> None:
        super().__init__()
        self._sm = session_manager
        self._current = current_id
        self._sessions: list = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Saved sessions", style="bold"), id="modal-title")
            yield OptionList(id="sessions")
            yield Static("", id="sessions-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Delete", id="delete", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        from pathlib import Path

        from novacode_cli.session.session_restore import format_session_age

        self._sessions = self._sm.list_sessions(limit=20)
        ol = self.query_one("#sessions", OptionList)
        ol.clear_options()
        hint = self.query_one("#sessions-hint", Static)
        if not self._sessions:
            hint.update(Text("No saved sessions.", style="dim"))
            return
        for meta in self._sessions:
            age = format_session_age(meta.last_active)
            project = Path(meta.project_root).name if meta.project_root else "no project"
            model = meta.model_name or "unknown"
            # Legacy sessions (no recorded provider) cannot have their model
            # rebuilt on resume — mark them so the label never promises a restore.
            if meta.model_name and not getattr(meta, "model_provider", None):
                model = f"{model} (not restored)"
            marker = " ← current" if meta.session_id == self._current else ""
            ol.add_option(
                Option(
                    f"{meta.session_id[:8]}{marker}  ·  {project} ({model})  ·  "
                    f"{meta.message_count} msgs  ·  {age}"
                )
            )
        hint.update(Text("Resume with:  nova --continue <id>", style="dim"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "delete":
            self._delete_highlighted()

    @work
    async def _delete_highlighted(self) -> None:
        ol = self.query_one("#sessions", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._sessions)):
            return
        meta = self._sessions[idx]
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete session {meta.session_id[:8]}?",
                Text("This cannot be undone."),
            )
        )
        if ok:
            try:
                self._sm.delete_session(meta.session_id)
            except Exception:  # noqa: BLE001
                pass
            self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class McpScreen(ModalScreen[None]):
    """Browse configured MCP servers and install presets.

    Shows configured servers with active/inactive status, the full catalog of
    available presets, and lets users install presets (collecting API keys
    when needed) or remove configured servers — all in the TUI."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._config_names: list[str] = []
        self._preset_ids: list[str] = []
        self._presets: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("MCP Management", style="bold"), id="modal-title")
            yield Static(Text("Configured Servers:", style="bold cyan"), id="mcp-section")
            yield OptionList(id="mcp-configured")
            yield Static(Text("Available Presets:", style="bold yellow"), id="preset-section")
            yield OptionList(id="mcp-presets")
            yield Static("", id="mcp-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Install", id="install", variant="primary")
                yield Button("Add Custom", id="add-custom")
                yield Button("Toggle Active", id="toggle-enable")
                yield Button("Remove", id="remove", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._reload()
        # Discover live MCP servers OFF the UI thread (it can connect to stdio
        # servers and block/hang). Repaint active status when it finishes.
        try:
            from novacode_cli.mcp import get_shared_mcp_middleware

            if not get_shared_mcp_middleware()._tools_discovered:
                self._discover_then_refresh()
        except Exception:  # noqa: BLE001
            pass

    @work(thread=True, exclusive=True)
    def _discover_then_refresh(self) -> None:
        """Run MCP discovery in a worker thread, then repaint on the UI thread."""
        try:
            from novacode_cli.mcp import get_shared_mcp_middleware

            get_shared_mcp_middleware()._discover_tools_sync()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.app.call_from_thread(self._reload)
        except Exception:  # noqa: BLE001
            pass

    def _reload(self) -> None:
        """Refresh both configured servers and presets lists (non-blocking)."""
        # Configured servers
        configured = self.query_one("#mcp-configured", OptionList)
        configured.clear_options()
        self._config_names = []
        try:
            from novacode_cli.mcp.config import MCPConfig

            servers = MCPConfig().list_servers()
        except Exception:  # noqa: BLE001
            servers = {}

        # Active servers from whatever has ALREADY been discovered (no blocking
        # discovery here — _discover_then_refresh fills this in asynchronously).
        active_servers: set[str] = set()
        try:
            from novacode_cli.mcp import get_shared_mcp_middleware

            middleware = get_shared_mcp_middleware()
            for tool_meta in middleware._tools_cache:
                sn = tool_meta.get("server")
                if sn:
                    active_servers.add(sn)
        except Exception:  # noqa: BLE001
            pass

        if servers:
            for name, sc in servers.items():
                self._config_names.append(name)
                is_disabled = getattr(sc, "disabled", False)
                is_active = name in active_servers and not is_disabled
                transport = getattr(sc, "transport", "?")
                loc = getattr(sc, "url", None) or getattr(sc, "command", None) or ""
                if is_disabled:
                    status_mark = "\u25cb"
                    status_style = "#f7768e"
                    label = Text.assemble(
                        (f"{status_mark}  {name} (disabled)  \u00b7  ", status_style),
                        (f"{transport}  \u00b7  {loc}", "dim strike"),
                    )
                else:
                    status_mark = "\u25cf" if is_active else "\u25cb"
                    status_style = "#73daca" if is_active else "#565f89"
                    label = Text.assemble(
                        (f"{status_mark}  {name}  \u00b7  ", status_style),
                        (f"{transport}  \u00b7  {loc}", "dim"),
                    )
                configured.add_option(Option(label))

            # Show active tool count
            active_section = self.query_one("#mcp-section", Static)
            active_count = sum(
                1
                for n in active_servers
                if n in self._config_names and not getattr(servers[n], "disabled", False)
            )
            disabled_count = sum(
                1 for n in self._config_names if getattr(servers[n], "disabled", False)
            )
            inactive_count = len(self._config_names) - active_count - disabled_count

            counts_str = []
            if active_count:
                counts_str.append(f"{active_count} active")
            if inactive_count:
                counts_str.append(f"{inactive_count} inactive")
            if disabled_count:
                counts_str.append(f"{disabled_count} disabled")

            counts_text = f" ({', '.join(counts_str)})" if counts_str else ""
            active_section.update(
                Text(
                    f"Configured Servers: {len(self._config_names)} total" + counts_text,
                    style="bold cyan",
                )
            )
        else:
            configured.add_option(Option("(no MCP servers configured)"))
            self.query_one("#mcp-section", Static).update(
                Text("Configured Servers: (none)", style="bold cyan")
            )

        # Update toggle button dynamically based on reload
        self._update_toggle_button(configured.highlighted)

        # Available presets
        presets_list = self.query_one("#mcp-presets", OptionList)
        presets_list.clear_options()
        self._preset_ids = []
        self._presets = {}
        try:
            from novacode_cli.mcp.presets import list_presets

            self._presets = list_presets()
        except Exception:  # noqa: BLE001
            pass

        for pid, preset in self._presets.items():
            self._preset_ids.append(pid)
            already_configured = pid in self._config_names
            marker = "[\u2713]" if already_configured else "[ ]"
            needs_key = "\U0001f511" if preset.get("setup_key") else ""
            label = Text.assemble(
                (f"{marker}  {pid}  ", "bold #e0af68"),
                (f"{preset['name']}", ""),
                (f"  {needs_key}", "dim") if needs_key else ("", ""),
                (f"\n     {preset['description']}", "dim"),
            )
            presets_list.add_option(Option(label))

        hint = self.query_one("#mcp-hint", Static)
        hint.update(
            Text(
                f"{len(self._presets)} presets \u00b7 "
                "\U0001f511 = needs API key \u00b7 [\u2713] = already configured \u00b7 "
                "restart session after installing",
                style="dim",
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "remove":
            self._remove_highlighted()
        elif event.button.id == "install":
            self._install_highlighted()
        elif event.button.id == "add-custom":
            self._add_custom()
        elif event.button.id == "toggle-enable":
            self._toggle_highlighted()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "mcp-configured":
            self._update_toggle_button(event.option_index)

    def _update_toggle_button(self, idx: int | None) -> None:
        try:
            btn = self.query_one("#toggle-enable", Button)
            if idx is None or not (0 <= idx < len(self._config_names)):
                btn.label = "Toggle Active"
                btn.disabled = True
                return

            btn.disabled = False
            name = self._config_names[idx]
            from novacode_cli.mcp.config import MCPConfig

            sc = MCPConfig().get_server(name)
            if sc and getattr(sc, "disabled", False):
                btn.label = "Activate"
            else:
                btn.label = "Deactivate"
        except Exception:
            pass

    @work
    async def _toggle_highlighted(self) -> None:
        if not self._config_names:
            return
        ol = self.query_one("#mcp-configured", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._config_names)):
            return
        name = self._config_names[idx]
        try:
            from novacode_cli.mcp.config import MCPConfig

            config_mgr = MCPConfig()
            sc = config_mgr.get_server(name)
            if sc:
                sc.disabled = not getattr(sc, "disabled", False)
                await config_mgr.add_server_async(name, sc)

                # Show status message
                state_str = "deactivated" if sc.disabled else "activated"
                self.app._log(
                    Text(
                        f"\u2713 MCP '{name}' {state_str}!",
                        style="green",
                    )
                )
                if hasattr(self.app, "session_state") and hasattr(
                    self.app.session_state, "reload_mcp_servers"
                ):
                    new_agent, new_backend = await self.app.session_state.reload_mcp_servers()
                    self.app.agent = new_agent
                    self.app.backend = new_backend
        except Exception as ex:  # noqa: BLE001
            self.app._log(Text(f"Toggle failed: {ex}", style="red"))
        self._reload()

    @work
    async def _remove_highlighted(self) -> None:
        if not self._config_names:
            return
        ol = self.query_one("#mcp-configured", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._config_names)):
            return
        name = self._config_names[idx]
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                f"Remove MCP server '{name}'?",
                Text("This edits ~/.nova/mcp.json."),
            )
        )
        if ok:
            try:
                from novacode_cli.mcp.config import MCPConfig

                MCPConfig().remove_server(name)
                self.app._log(
                    Text(
                        f"\u2713 MCP '{name}' removed!",
                        style="green",
                    )
                )
                if hasattr(self.app, "session_state") and hasattr(
                    self.app.session_state, "reload_mcp_servers"
                ):
                    new_agent, new_backend = await self.app.session_state.reload_mcp_servers()
                    self.app.agent = new_agent
                    self.app.backend = new_backend
            except Exception as ex:  # noqa: BLE001
                self.app._log(Text(f"Remove failed: {ex}", style="red"))
            self._reload()

    @work
    async def _install_highlighted(self) -> None:
        """Install the highlighted preset, collecting API keys when needed."""
        ol = self.query_one("#mcp-presets", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._preset_ids)):
            return
        preset_id = self._preset_ids[idx]
        preset = self._presets.get(preset_id)
        if preset is None:
            return

        # Already configured?
        if preset_id in self._config_names:
            ok = await self.app.push_screen_wait(
                ConfirmModal(
                    f"'{preset['name']}' is already configured.",
                    Text("Re-install with new configuration?"),
                )
            )
            if not ok:
                return

        # Collect API keys / user inputs
        user_inputs: dict[str, str] = {}
        if preset.get("setup_key"):
            result = await self.app.push_screen_wait(McpInstallModal(preset, preset_id))
            if result is None:  # cancelled
                return
            user_inputs = result

        # Create and save config
        try:
            from novacode_cli.mcp.config import MCPConfig
            from novacode_cli.mcp.presets import create_config_from_preset

            config = create_config_from_preset(preset_id, user_inputs)
            if config:
                MCPConfig().add_server(preset_id, config)
                self.app._log(
                    Text(
                        f"\u2713 MCP preset '{preset['name']}' installed!",
                        style="green",
                    )
                )
                if hasattr(self.app, "session_state") and hasattr(
                    self.app.session_state, "reload_mcp_servers"
                ):
                    new_agent, new_backend = await self.app.session_state.reload_mcp_servers()
                    self.app.agent = new_agent
                    self.app.backend = new_backend
        except Exception as ex:  # noqa: BLE001
            self.app._log(Text(f"Install failed: {ex}", style="red"))
        self._reload()

    @work
    async def _add_custom(self) -> None:
        """Show a two-step modal for adding a custom MCP server."""
        result = await self.app.push_screen_wait(McpCustomModal())
        if result is not None:
            try:
                from novacode_cli.mcp.config import MCPConfig, MCPServerConfig

                name = result["name"]
                if result["transport"] == "http":
                    config = MCPServerConfig(
                        transport="http",
                        url=result["url"],
                        description=result.get("description") or None,
                    )
                else:
                    config = MCPServerConfig(
                        transport="stdio",
                        command=result["command"],
                        args=result.get("args") or [],
                        env=result.get("env") or {},
                        description=result.get("description") or None,
                    )
                MCPConfig().add_server(name, config)
                self.app._log(
                    Text(
                        f"\u2713 Custom MCP '{name}' added!",
                        style="green",
                    )
                )
                if hasattr(self.app, "session_state") and hasattr(
                    self.app.session_state, "reload_mcp_servers"
                ):
                    new_agent, new_backend = await self.app.session_state.reload_mcp_servers()
                    self.app.agent = new_agent
                    self.app.backend = new_backend
            except Exception as ex:  # noqa: BLE001
                self.app._log(Text(f"Add custom MCP failed: {ex}", style="red"))
            self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class McpInstallModal(ModalScreen[dict | None]):
    """Collect API keys / configuration values for an MCP preset before installing."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, preset: dict, preset_id: str) -> None:
        super().__init__()
        self._preset = preset
        self._preset_id = preset_id

    def compose(self) -> ComposeResult:
        preset = self._preset
        with Vertical(id="modal-box"):
            yield Static(
                Text(f"Install: {preset['name']}", style="bold #e0af68"),
                id="modal-title",
            )
            yield Static(
                Text(
                    f"{preset.get('description', '')}\nPackage: {preset.get('package', 'N/A')}",
                    style="dim",
                ),
                id="modal-body",
            )
            if preset.get("setup_prompt"):
                yield Static(Text(preset["setup_prompt"], style="bold"), id="mcp-key-label")
                yield Input(placeholder="Enter value\u2026", id="mcp-key-value", password=True)
            yield Static("", id="mcp-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Install", id="do-install", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self._preset.get("setup_prompt"):
            self.query_one("#mcp-key-value", Input).focus()
        else:
            self.query_one("#do-install", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "do-install":
            self._do_install()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "mcp-key-value":
            self._do_install()

    def _do_install(self) -> None:
        result: dict[str, str] = {}
        key = self._preset.get("setup_key")
        if key:
            try:
                val = self.query_one("#mcp-key-value", Input).value.strip()
            except Exception:  # noqa: BLE001
                val = ""
            if not val:
                return  # don't install without required key
            result[key] = val
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


class McpCustomModal(ModalScreen[dict | None]):
    """Two-step modal to add a custom MCP server (name + transport + connection details)."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Add Custom MCP Server", style="bold"), id="modal-title")
            yield Static(Text("Server name:", style="bold"), id="mcp-name-label")
            yield Input(placeholder="e.g., my-custom-mcp", id="mcp-name")
            yield Static(Text("Transport:", style="bold"), id="mcp-transport-label")
            yield Select(
                [("stdio (local command)", "stdio"), ("HTTP (remote server)", "http")],
                id="mcp-transport",
                value="stdio",
            )
            yield Static(Text("Connection:", style="bold"), id="mcp-conn-label")
            yield Input(
                placeholder="npx -y @scope/package  OR  https://example.com/mcp",
                id="mcp-conn",
            )
            yield Input(placeholder="Description (optional)", id="mcp-desc")
            yield Static("", id="mcp-custom-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Add", id="do-add", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self.query_one("#mcp-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "do-add":
            self._do_add()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "mcp-name":
            self.query_one("#mcp-conn", Input).focus()
        elif event.input.id == "mcp-conn":
            self._do_add()

    def _do_add(self) -> None:
        hint = self.query_one("#mcp-custom-hint", Static)
        try:
            name = self.query_one("#mcp-name", Input).value.strip()
        except Exception:  # noqa: BLE001
            name = ""
        if not name:
            hint.update(Text("Server name is required", style="red"))
            return
        try:
            transport = self.query_one("#mcp-transport", Select).value
        except Exception:  # noqa: BLE001
            transport = "stdio"
        try:
            conn = self.query_one("#mcp-conn", Input).value.strip()
        except Exception:  # noqa: BLE001
            conn = ""
        if not conn:
            hint.update(Text("Connection string/URL is required", style="red"))
            return
        try:
            desc = self.query_one("#mcp-desc", Input).value.strip()
        except Exception:  # noqa: BLE001
            desc = ""

        if str(transport) == "http":
            result: dict = {"name": name, "transport": "http", "url": conn}
        else:
            # stdio: split command into executable + args
            parts = conn.split()
            cmd = parts[0] if parts else conn
            # Validate command locally first to show immediate feedback in modal!
            try:
                from novacode_cli.mcp.config import MCPServerConfig

                MCPServerConfig.validate_command(cmd)
            except ValueError as ex:
                hint.update(Text(f"Validation failed: {ex}", style="red"))
                return

            result = {
                "name": name,
                "transport": "stdio",
                "command": cmd,
                "args": parts[1:] if len(parts) > 1 else [],
            }
        if desc:
            result["description"] = desc
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InfoListScreen(ModalScreen[None]):
    """A simple read-only list modal (used by /skills and /agents)."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title: str, lines: list[str], hint: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._lines = lines
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style="bold"), id="modal-title")
            yield OptionList(id="infolist")
            if self._hint:
                yield Static(Text(self._hint, style="dim"), id="info-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        ol = self.query_one("#infolist", OptionList)
        for line in self._lines:
            ol.add_option(Option(line))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class PickScreen(ModalScreen[int]):
    """A reusable selection modal: choose one item, returns its index (-1 = cancel)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: list[str], hint: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style="bold"), id="modal-title")
            yield OptionList(id="pick-list")
            if self._hint:
                yield Static(Text(self._hint, style="dim"), id="pick-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Select", id="select", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        ol = self.query_one("#pick-list", OptionList)
        for line in self._options:
            ol.add_option(Option(line))
        if self._options:
            ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(-1)
        elif event.button.id == "select":
            ol = self.query_one("#pick-list", OptionList)
            self.dismiss(ol.highlighted if ol.highlighted is not None else -1)

    def action_cancel(self) -> None:
        self.dismiss(-1)


class PluginsScreen(ModalScreen[None]):
    """Native ``/plugins`` manager: list installed plugins and toggle them.

    Enable/disable writes ``~/.nova/plugins/manifest.json``; the agent reads it
    at build time, so a session restart is needed for changes to take effect.
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._plugins: list = []  # list of (name, spec)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Middleware", style="bold"), id="modal-title")
            yield OptionList(id="plugins")
            yield Static("", id="plugins-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Enable / Disable", id="toggle", variant="success")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    @staticmethod
    def _counts(spec: Any) -> str:
        if not isinstance(spec, dict):
            return ""
        parts = []
        for key, label in (
            ("middleware", "mw"),
            ("tools", "t"),
            ("subagents", "sa"),
            ("commands", "cmd"),
        ):
            n = len(spec.get(key, []) or [])
            if n:
                parts.append(f"{n}{label}")
        return " · ".join(parts)

    def _reload(self) -> None:
        from novacode_cli.plugins.loader import (
            discover_plugins,
            list_enabled_plugins,
        )

        try:
            self._plugins = discover_plugins()
        except Exception:  # noqa: BLE001
            self._plugins = []
        enabled = set(list_enabled_plugins())

        ol = self.query_one("#plugins", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        hint = self.query_one("#plugins-hint", Static)

        if not self._plugins:
            hint.update(
                Text(
                    "No plugins installed.  pip install one (it registers a "
                    "'nova.plugins' entry point), then reopen /plugins.",
                    style="dim",
                )
            )
            return

        for name, spec in self._plugins:
            on = name in enabled
            opt = Text()
            opt.append("✓ enabled  " if on else "○ disabled ", style="green" if on else "dim")
            opt.append(str(name), style="bold")
            desc = spec.get("description", "") if isinstance(spec, dict) else ""
            if desc:
                opt.append(f"  — {desc}")
            counts = self._counts(spec)
            if counts:
                opt.append(f"   [{counts}]", style="dim")
            ol.add_option(Option(opt))

        if keep is not None and 0 <= keep < len(self._plugins):
            ol.highlighted = keep
        else:
            ol.highlighted = 0
        ol.focus()
        hint.update(
            Text(
                "Enter or Enable/Disable to toggle · restart the session to apply",
                style="dim",
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._toggle(event.option_index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "toggle":
            ol = self.query_one("#plugins", OptionList)
            if ol.highlighted is not None:
                self._toggle(ol.highlighted)

    def _toggle(self, idx: int) -> None:
        if not 0 <= idx < len(self._plugins):
            return
        from novacode_cli.plugins.loader import (
            disable_plugin,
            enable_plugin,
            list_enabled_plugins,
        )

        name = self._plugins[idx][0]
        if name in set(list_enabled_plugins()):
            disable_plugin(name)
        else:
            enable_plugin(name)
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class ClaudePluginsScreen(ModalScreen[None]):
    """Native ``/plugins`` viewer: installed Claude-compatible plugins.

    Lists plugins under ``~/.nova/plugins`` (installed directly or via a
    marketplace) with a scrollable per-plugin component breakdown — readable even
    when a plugin ships 200+ skills. ``r`` / Remove uninstalls the highlighted
    plugin; install/search stay on the ``/plugins`` command.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("r", "remove", "Remove"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._plugins: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Plugins", style="bold"), id="modal-title")
            yield OptionList(id="cplugins-list")
            yield Static(Text("Components:", style="bold yellow"), id="cplugin-detail-header")
            yield Static("", id="cplugin-detail", classes="preview-box")
            yield Static("", id="cplugins-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Remove", id="remove", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    @staticmethod
    def _counts(comps: dict) -> str:
        parts = []
        for key, label in (
            ("skills", "skills"),
            ("commands", "cmds"),
            ("agents", "agents"),
            ("mcp", "mcp"),
            ("hooks", "hooks"),
        ):
            n = len(comps.get(key, []) or [])
            if n:
                parts.append(f"{n} {label}")
        return " · ".join(parts)

    def _reload(self) -> None:
        from novacode_cli.plugins import claude_plugins as cp

        try:
            self._plugins = cp.list_plugins()
        except Exception:  # noqa: BLE001
            self._plugins = []

        ol = self.query_one("#cplugins-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        hint = self.query_one("#cplugins-hint", Static)

        if not self._plugins:
            self.query_one("#cplugin-detail", Static).update(
                Text("No plugins installed.", style="dim")
            )
            hint.update(
                Text(
                    "Install with  /plugins install <owner/repo | plugin@marketplace>",
                    style="dim",
                )
            )
            return

        for p in self._plugins:
            comps = cp.plugin_components(p["name"])
            row = Text()
            row.append("● ", style="green" if p.get("enabled", True) else "dim")
            row.append(str(p["name"]), style="bold")
            counts = self._counts(comps)
            if counts:
                row.append(f"   {counts}", style="dim")
            ol.add_option(Option(row))

        if keep is not None and 0 <= keep < len(self._plugins):
            ol.highlighted = keep
        else:
            ol.highlighted = 0
        ol.focus()
        self._update_detail(ol.highlighted)
        hint.update(
            Text(
                f"{len(self._plugins)} plugin(s) · r removes the highlighted one · "
                "/reload-plugins applies changes this session",
                style="dim",
            )
        )

    def _update_detail(self, idx: int | None) -> None:
        from novacode_cli.plugins import claude_plugins as cp

        detail = self.query_one("#cplugin-detail", Static)
        if idx is None or not (0 <= idx < len(self._plugins)):
            detail.update(Text(""))
            return
        p = self._plugins[idx]
        comps = cp.plugin_components(p["name"])
        t = Text()
        t.append(str(p["name"]), style="bold #7aa2f7")
        t.append(f"\n{p.get('source', '')}", style="dim")
        t.append(f"\n{p.get('path', '')}", style="dim")
        for key, label in (
            ("skills", "Skills"),
            ("commands", "Commands"),
            ("agents", "Agents"),
            ("mcp", "MCP servers"),
            ("hooks", "Hooks"),
        ):
            items = comps.get(key, []) or []
            if not items:
                continue
            t.append(f"\n\n{label} ({len(items)})", style="bold cyan")
            t.append("\n" + ", ".join(str(i) for i in items))
        detail.update(t)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "cplugins-list":
            self._update_detail(event.option_index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "remove":
            self.action_remove()

    def action_remove(self) -> None:
        ol = self.query_one("#cplugins-list", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._plugins)):
            return
        name = self._plugins[idx]["name"]
        from novacode_cli.plugins import claude_plugins as cp

        try:
            cp.remove(name)
        except Exception as e:  # noqa: BLE001
            self.query_one("#cplugins-hint", Static).update(
                Text(f"Remove failed: {e}", style="red")
            )
            return
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class ThemeScreen(ModalScreen[None]):
    """Browse and apply Textual themes with live preview."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._theme_names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Choose Theme", style="bold"), id="modal-title")
            yield OptionList(id="theme-list")
            yield Static("", id="theme-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Apply", id="apply", variant="primary")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    def _reload(self) -> None:
        # available_themes includes builtins *and* Nova's registered tokyo-night.
        themes = self.app.available_themes

        ol = self.query_one("#theme-list", OptionList)
        ol.clear_options()
        self._theme_names = sorted(themes.keys())
        current = self.app.theme if hasattr(self.app, "theme") else DEFAULT_THEME
        for name in self._theme_names:
            # Just the theme name, with a dot marking the active one.
            is_current = name == current
            marker = "\u25cf" if is_current else "\u25cb"
            label = Text(
                f"{marker}  {name}",
                style="bold" if is_current else "",
            )
            ol.add_option(Option(label))

        # Highlight current theme
        try:
            idx = self._theme_names.index(current)
            ol.highlighted = idx
        except ValueError:
            pass

        hint = self.query_one("#theme-hint", Static)
        hint.update(
            Text(
                f"{len(self._theme_names)} themes \u00b7 changes apply instantly \u00b7 persist across restarts",
                style="dim",
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "theme-list":
            self._do_apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "apply":
            self._do_apply()

    def _do_apply(self) -> None:
        ol = self.query_one("#theme-list", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._theme_names)):
            return
        name = self._theme_names[idx]
        # Apply instantly — setting App.theme re-renders all themed CSS.
        self.app.theme = name
        # Persist to the same config the app reads on startup (Nova.config.json).
        try:
            from novacode_cli.config.nova_config import NovaConfig

            NovaConfig().set("theme", name)
        except Exception:  # noqa: BLE001
            pass
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


def _tools_summary(tools: list[str] | None) -> str:
    if tools is None:
        return "all tools"
    if not tools:
        return "file + shell tools only"
    shown = ", ".join(tools[:6])
    return f"{len(tools)}: {shown}" + (f", +{len(tools) - 6} more" if len(tools) > 6 else "")


class ToolPickerModal(ModalScreen[dict | None]):
    """Pick the tools a subagent gets. Dismisses ``{"tools": list | None}``
    (None = no restriction, every tool) or ``None`` on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ToolPickerModal #modal-box { height: 90%; }
    ToolPickerModal #tool-list { height: 1fr; border: round $accent 50%; }
    ToolPickerModal #modal-buttons { dock: bottom; }
    """

    def __init__(
        self, title: str, arsenal: list[tuple[str, str]], selected: list[str] | None
    ) -> None:
        super().__init__()
        self._title = title
        self._arsenal = arsenal
        names = {n for n, _ in arsenal}
        self._selected: set[str] = set(names) if selected is None else set(selected) & names

    def compose(self) -> ComposeResult:
        from novacode_cli.agents.agent_file import ALWAYS_INCLUDED

        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style="bold"), id="modal-title")
            yield Static(
                Text(
                    f"Always included: {', '.join(ALWAYS_INCLUDED)}. "
                    "Space toggles, type to filter.",
                    style="dim",
                )
            )
            yield Input(placeholder="Filter tools...", id="tool-filter")
            yield SelectionList[str](id="tool-list")
            yield Static("", id="tool-count")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Select shown", id="all-shown")
                yield Button("Clear shown", id="none-shown")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._fill("")
        self.query_one("#tool-filter", Input).focus()

    def _fill(self, needle: str) -> None:
        sl = self.query_one("#tool-list", SelectionList)
        sl.clear_options()
        needle = needle.lower().strip()
        for name, desc in self._arsenal:
            if needle and needle not in name.lower() and needle not in desc.lower():
                continue
            label = Text.assemble((name, "bold"), ("  " + desc[:70], "dim"))
            sl.add_option((label, name, name in self._selected))
        self._count()

    def _count(self) -> None:
        total = len(self._arsenal)
        n = len(self._selected)
        note = " (every tool)" if n == total else ""
        self.query_one("#tool-count", Static).update(
            Text(f"{n} of {total} selected{note}", style="cyan")
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tool-filter":
            self._fill(event.value)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        sl = event.selection_list
        shown = {sl.get_option_at_index(i).value for i in range(sl.option_count)}
        self._selected = (self._selected - shown) | set(sl.selected)
        self._count()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        sl = self.query_one("#tool-list", SelectionList)
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "all-shown":
            sl.select_all()
        elif event.button.id == "none-shown":
            sl.deselect_all()
        elif event.button.id == "save":
            # Everything ticked means "no restriction", so tools added later
            # (a new MCP server) reach this agent too.
            everything = self._selected >= {n for n, _ in self._arsenal}
            self.dismiss({"tools": None if everything else sorted(self._selected)})

    def action_cancel(self) -> None:
        self.dismiss(None)


def _session_arsenal(app: Any) -> list[tuple[str, str]]:
    from novacode_cli.agents.agent_file import tool_arsenal

    return tool_arsenal(getattr(getattr(app, "session_state", None), "_tools", None))


class AgentCreateModal(ModalScreen[dict | None]):
    """Modal dialog to collect inputs for creating a new custom subagent."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AgentCreateModal #modal-box { height: 90%; }
    AgentCreateModal #agent-form { height: 1fr; }
    AgentCreateModal #modal-buttons { dock: bottom; }
    AgentCreateModal #agent-tools-row { height: auto; margin-top: 1; }
    AgentCreateModal #agent-tools-summary { width: 1fr; padding: 1 1 0 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tools: list[str] | None = None  # None = every tool

    def compose(self) -> ComposeResult:
        from novacode_cli.config.config import settings

        scope_options = [("Global (all projects)", "global")]
        if settings.project_root is not None:
            scope_options.append(("Project (current project only)", "project"))

        with Vertical(id="modal-box"):
            yield Static(Text("Create Custom Subagent", style="bold"), id="modal-title")
            with VerticalScroll(id="agent-form"):
                yield Static(
                    Text("Agent Name (e.g. code-reviewer):", style="bold"), id="agent-name-label"
                )
                yield Input(
                    placeholder="Name (letters, numbers, hyphens, underscores)", id="agent-name"
                )
                yield Static(
                    Text("Specialization / Description:", style="bold"), id="agent-desc-label"
                )
                yield Input(
                    placeholder="e.g. Reviews python code for security vulnerabilities",
                    id="agent-desc",
                )
                yield Static(Text("Storage Scope:", style="bold"), id="agent-scope-label")
                yield Select(scope_options, id="agent-scope", value="global")
                yield Static(Text("Color Theme:", style="bold"), id="agent-color-label")
                yield Select(
                    [
                        ("Sky Blue", "#0ea5e9"),
                        ("Teal", "#14b8a6"),
                        ("Green", "#22c55e"),
                        ("Blue", "#3b82f6"),
                        ("Orange", "#f97316"),
                        ("Red", "#ef4444"),
                        ("Purple", "#a855f7"),
                        ("Pink", "#ec4899"),
                        ("Gray", "#6b7280"),
                    ],
                    id="agent-color",
                    value="#0ea5e9",
                )
                yield Static(Text("Tools:", style="bold"), id="agent-tools-label")
                with Horizontal(id="agent-tools-row"):
                    yield Static(_tools_summary(None), id="agent-tools-summary")
                    yield Button("Choose tools...", id="choose-tools")
            yield Static("", id="agent-create-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Create", id="do-create", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self.query_one("#agent-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "do-create":
            self._submit()
        elif event.button.id == "choose-tools":
            self.app.push_screen(
                ToolPickerModal(
                    "Tools for the new subagent", _session_arsenal(self.app), self._tools
                ),
                callback=self._tools_chosen,
            )

    def _tools_chosen(self, result: dict | None) -> None:
        if result is None:
            return
        self._tools = result["tools"]
        self.query_one("#agent-tools-summary", Static).update(_tools_summary(self._tools))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "agent-name":
            self.query_one("#agent-desc", Input).focus()
        elif event.input.id == "agent-desc":
            self._submit()

    def _submit(self) -> None:
        hint = self.query_one("#agent-create-hint", Static)
        try:
            name = self.query_one("#agent-name", Input).value.strip()
        except Exception:
            name = ""
        if not name:
            hint.update(Text("Agent name is required", style="red"))
            return

        from novacode_cli.config.config import settings

        if not settings._is_valid_agent_name(name):
            hint.update(
                Text("Invalid name (use letters, numbers, hyphens, underscores)", style="red")
            )
            return

        try:
            desc = self.query_one("#agent-desc", Input).value.strip()
        except Exception:
            desc = ""
        if not desc:
            hint.update(Text("Description is required", style="red"))
            return

        try:
            scope = self.query_one("#agent-scope", Select).value
        except Exception:
            scope = "global"

        try:
            color = self.query_one("#agent-color", Select).value
        except Exception:
            color = "#0ea5e9"

        self.dismiss(
            {
                "name": name,
                "description": desc,
                "scope": scope,
                "color": color,
                "tools": self._tools,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class AgentsScreen(ModalScreen[None]):
    """Native subagents manager: list configured agents, view details, create or delete them."""

    BINDINGS = [("escape", "close", "Close")]
    # Fixed height with the buttons docked: the details pane holds a whole
    # system prompt, and with height:auto it pushed Create/Delete off the modal.
    DEFAULT_CSS = """
    AgentsScreen #modal-box { height: 90%; }
    AgentsScreen #agents-list { height: 1fr; max-height: 100%; min-height: 3; }
    AgentsScreen #agent-detail-scroll {
        height: 2fr; background: $boost; border: round $accent 50%; padding: 0 1;
    }
    AgentsScreen #modal-buttons { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._agents: list[tuple[str, Path, str]] = []  # list of (name, path, scope)
        self._generating = False

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Subagents Manager", style="bold"), id="modal-title")
            yield Static(Text("Configured Subagents:", style="bold cyan"), id="agents-section")
            yield OptionList(id="agents-list")
            yield Static(Text("Subagent Details:", style="bold yellow"), id="agent-detail-header")
            with VerticalScroll(id="agent-detail-scroll"):
                yield Static("", id="agent-detail-preview")
            yield Static("", id="agents-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Create", id="create", variant="primary")
                yield Button("Tools", id="edit-tools")
                yield Button("Delete", id="delete", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    def _reload(self) -> None:
        from novacode_cli.config.config import settings

        try:
            self._agents = sorted(settings.get_all_agents(), key=lambda x: x[0].lower())
        except Exception:
            self._agents = []

        ol = self.query_one("#agents-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        hint = self.query_one("#agents-hint", Static)

        if not self._agents:
            ol.add_option(Option("(no custom subagents found)"))
            self.query_one("#agent-detail-preview", Static).update(
                Text("No subagents are currently configured.", style="dim")
            )
            self.query_one("#delete", Button).disabled = True
            self.query_one("#edit-tools", Button).disabled = True
            hint.update(Text("Create one with the Create button", style="dim"))
            return

        self.query_one("#delete", Button).disabled = False
        self.query_one("#edit-tools", Button).disabled = False
        for name, path, scope in self._agents:
            from novacode_cli.commands.agents_commands import extract_agent_description

            desc = ""
            try:
                desc = extract_agent_description(path / "agent.md")
            except Exception:
                pass
            label = Text.assemble(
                (f"@{name} ", "bold #73daca"), (f" · {scope} · ", "dim"), (desc, "dim")
            )
            ol.add_option(Option(label))

        if keep is not None and 0 <= keep < len(self._agents):
            ol.highlighted = keep
        else:
            ol.highlighted = 0
        ol.focus()

        self._update_preview(ol.highlighted)
        hint.update(
            Text("Select an agent to see details · Create/Delete custom agents", style="dim")
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "agents-list":
            self._update_preview(event.option_index)

    def _update_preview(self, idx: int | None) -> None:
        preview = self.query_one("#agent-detail-preview", Static)
        if idx is None or not self._agents or not (0 <= idx < len(self._agents)):
            preview.update("")
            return

        name, path, scope = self._agents[idx]
        from novacode_cli.commands.agents_commands import extract_agent_description

        desc = ""
        system_prompt = ""
        color = ""
        agent_md = path / "agent.md"
        try:
            from novacode_cli.agents.agent_file import read_agent

            desc = extract_agent_description(agent_md)
            front, system_prompt = read_agent(agent_md)
            system_prompt = system_prompt.strip()
            color = str(front.get("color") or "")
        except Exception as e:
            system_prompt = f"(error reading system prompt: {e})"

        preview_text = Text()
        preview_text.append(f"Name: ", style="bold")
        preview_text.append(f"@{name}\n", style="bold #73daca")
        preview_text.append(f"Scope: ", style="bold")
        preview_text.append(
            f"{scope}\n", style="bold yellow" if scope == "project" else "bold blue"
        )
        if color:
            preview_text.append(f"Color: ", style="bold")
            preview_text.append(f"{color}\n", style=f"bold {color}")
        if desc:
            preview_text.append(f"Description: ", style="bold")
            preview_text.append(f"{desc}\n", style="dim")
        try:
            from novacode_cli.agents.agent_file import agent_tools

            chosen = agent_tools(agent_md.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            chosen = None
        preview_text.append("Tools: ", style="bold")
        preview_text.append(f"{_tools_summary(chosen)}\n\n", style="cyan")
        preview_text.append(f"System Prompt:\n", style="bold")
        preview_text.append(system_prompt, style="italic dim")

        preview.update(preview_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "create":
            if not self._generating:
                self.app.run_worker(self._create_agent(), group="create_agent", exclusive=True)
        elif event.button.id == "delete":
            if not self._generating:
                self._delete_agent()
        elif event.button.id == "edit-tools":
            if not self._generating:
                self._edit_tools()

    def _edit_tools(self) -> None:
        """Change the highlighted agent's tools (writes its agent.md)."""
        idx = self.query_one("#agents-list", OptionList).highlighted
        if idx is None or not (0 <= idx < len(self._agents)):
            return
        name, path, _scope = self._agents[idx]
        agent_md = path / "agent.md"
        from novacode_cli.agents.agent_file import agent_tools, set_agent_tools

        try:
            current = agent_tools(agent_md.read_text(encoding="utf-8"))
        except OSError:
            return

        def _save(result: dict | None) -> None:
            if result is None:
                return
            try:
                set_agent_tools(agent_md, result["tools"])
            except Exception as e:  # noqa: BLE001
                self.query_one("#agents-hint", Static).update(
                    Text(f"Could not save tools: {e}", style="red")
                )
                return
            self.app._agent_names_cache = None
            self._update_preview(idx)
            self.query_one("#agents-hint", Static).update(
                Text(
                    f"Tools for @{name}: {_tools_summary(result['tools'])} "
                    "(applies from the next agent build: /clear or restart)",
                    style="green",
                )
            )

        self.app.push_screen(
            ToolPickerModal(f"Tools for @{name}", _session_arsenal(self.app), current),
            callback=_save,
        )

    async def _create_agent(self) -> None:
        result = await self.app.push_screen_wait(AgentCreateModal())
        if result is not None:
            name = result["name"]
            desc = result["description"]
            scope = result["scope"]
            color = result["color"]

            from novacode_cli.config.config import settings

            if scope == "project":
                agents_dir = settings.ensure_project_agents_dir()
                agent_dir = agents_dir / name
            else:
                agent_dir = settings.get_agents_root_dir() / name

            if agent_dir.exists():
                if self.is_mounted:
                    try:
                        hint = self.query_one("#agents-hint", Static)
                        hint.update(
                            Text(f"Agent '{name}' already exists in that scope.", style="yellow")
                        )
                    except Exception:
                        pass
                self.app._log(Text(f"Agent '{name}' already exists in that scope.", style="yellow"))
                return

            self._generating = True
            if self.is_mounted:
                try:
                    self.query_one("#create", Button).disabled = True
                    self.query_one("#delete", Button).disabled = True
                except Exception:
                    pass

                try:
                    hint = self.query_one("#agents-hint", Static)
                    hint.update(
                        Text(
                            f"Generating system prompt for @{name} using AI... Please wait.",
                            style="bold cyan",
                        )
                    )
                except Exception:
                    pass
            self.app._log(Text(f"Generating system prompt for @{name} using AI...", style="cyan"))

            try:
                from novacode_cli.commands.agents_commands import _generate_agent_system_prompt

                system_prompt = await _generate_agent_system_prompt(name, desc)
                if not system_prompt:
                    raise RuntimeError("AI generation of system prompt returned empty response.")

                from novacode_cli.agents.agent_file import write_agent

                agent_md = agent_dir / "agent.md"
                write_agent(
                    agent_md,
                    {"color": color, "description": desc, "tools": result.get("tools")},
                    system_prompt,
                )

                if self.is_mounted:
                    try:
                        hint = self.query_one("#agents-hint", Static)
                        hint.update(
                            Text(
                                f"✓ Custom subagent '@{name}' created successfully!", style="green"
                            )
                        )
                    except Exception:
                        pass
                self.app._log(
                    Text(f"✓ Custom subagent '@{name}' created successfully!", style="green")
                )
                self.app._agent_names_cache = None
            except Exception as e:
                if self.is_mounted:
                    try:
                        hint = self.query_one("#agents-hint", Static)
                        hint.update(Text(f"Failed to create agent: {e}", style="red"))
                    except Exception:
                        pass
                self.app._log(Text(f"Failed to create agent: {e}", style="red"))

            self._generating = False
            if self.is_mounted:
                try:
                    self.query_one("#create", Button).disabled = False
                    self.query_one("#delete", Button).disabled = False
                except Exception:
                    pass
                self._reload()

    @work
    async def _delete_agent(self) -> None:
        if self._generating:
            return
        ol = self.query_one("#agents-list", OptionList)
        idx = ol.highlighted
        if idx is None or not self._agents or not (0 <= idx < len(self._agents)):
            return

        name, path, scope = self._agents[idx]
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete custom subagent '@{name}'?",
                Text(
                    "This will delete the agent's folder and prompt config. This cannot be undone."
                ),
            )
        )
        if ok:
            import shutil

            try:
                shutil.rmtree(path)
                self.app._log(Text(f"✓ Deleted subagent '@{name}'!", style="green"))
                self.app._agent_names_cache = None
            except Exception as e:
                self.app._log(Text(f"Failed to delete subagent: {e}", style="red"))
            self._reload()

    def action_close(self) -> None:
        if self._generating:
            return
        self.dismiss(None)


class SkillCreateModal(ModalScreen[dict | None]):
    """Modal dialog to collect inputs for creating a new custom skill."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        from novacode_cli.config.config import settings

        scope_options = [("Global (all projects)", "global")]
        if settings.project_root is not None:
            scope_options.append(("Project (current project only)", "project"))

        with Vertical(id="modal-box"):
            yield Static(Text("Create Custom Skill", style="bold"), id="modal-title")
            yield Static(
                Text("Skill Name (e.g. docker-deploy):", style="bold"), id="skill-name-label"
            )
            yield Input(
                placeholder="Name (letters, numbers, hyphens, underscores)", id="skill-name"
            )

            yield Static(Text("Specialization / Description:", style="bold"), id="skill-desc-label")
            yield Input(placeholder="Description (optional)", id="skill-desc")

            yield Static(Text("Storage Scope:", style="bold"), id="skill-scope-label")
            yield Select(scope_options, id="skill-scope", value="global")

            yield Static("", id="skill-create-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Create", id="do-create", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self.query_one("#skill-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "do-create":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "skill-name":
            self.query_one("#skill-desc", Input).focus()
        elif event.input.id == "skill-desc":
            self._submit()

    def _submit(self) -> None:
        hint = self.query_one("#skill-create-hint", Static)
        try:
            name = self.query_one("#skill-name", Input).value.strip()
        except Exception:
            name = ""
        if not name:
            hint.update(Text("Skill name is required", style="red"))
            return

        from novacode_cli.skills.skill_creation import _validate_name

        is_valid, err = _validate_name(name)
        if not is_valid:
            hint.update(Text(f"Invalid name: {err}", style="red"))
            return

        try:
            desc = self.query_one("#skill-desc", Input).value.strip()
        except Exception:
            desc = ""

        try:
            scope = self.query_one("#skill-scope", Select).value
        except Exception:
            scope = "global"

        self.dismiss(
            {
                "name": name,
                "description": desc,
                "scope": scope,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class SkillsScreen(ModalScreen[None]):
    """Native skills manager: list/toggle/view installed skills, create new skills."""

    BINDINGS = [
        ("escape", "close", "Close"),
        ("space", "toggle", "Toggle on/off"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._generating = False
        # Which prefs file toggles are written to ("global" or "project").
        self._scope = "global"

    def compose(self) -> ComposeResult:
        from novacode_cli.config.config import settings

        in_project = settings.project_root is not None

        with Vertical(id="modal-box"):
            yield Static(Text("Skills Manager", style="bold"), id="modal-title")
            yield Static(Text("Installed Skills:", style="bold cyan"), id="skills-section")
            if in_project:
                yield Select(
                    [("Global (all projects)", "global"), ("Project (this repo)", "project")],
                    id="skill-scope-toggle",
                    value="global",
                    allow_blank=False,
                )
            yield OptionList(id="skills-list")
            yield Static(Text("Skill Details:", style="bold yellow"), id="skill-detail-header")
            yield Static("", id="skill-detail-preview", classes="preview-box")
            yield Static("", id="skills-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Toggle", id="toggle", variant="primary")
                yield Button("Create", id="create")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    def _reload(self) -> None:
        self.app._skill_names_cache = None
        names = self.app._get_skill_names()

        ol = self.query_one("#skills-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        hint = self.query_one("#skills-hint", Static)

        if not names:
            ol.add_option(Option("(no skills found)"))
            self.query_one("#skill-detail-preview", Static).update(
                Text("No skills are currently installed.", style="dim")
            )
            hint.update(Text("Create one with the Create button", style="dim"))
            return

        # Mark each skill on/off for the selected scope. [x] = on (passed to the
        # agent), [ ] = off (hidden). Per-scope view, mirroring the REPL board.
        from novacode_cli.skills import skills_prefs

        disabled = skills_prefs.load_disabled(skills_prefs.scope_path(self._scope))
        for name in names:
            on = name not in disabled
            row = Text()
            row.append("[x] " if on else "[ ] ", style="bold green" if on else "bold red")
            row.append(name, style="bold #e0af68" if on else "dim")
            ol.add_option(Option(row))

        if keep is not None and 0 <= keep < len(names):
            ol.highlighted = keep
        else:
            ol.highlighted = 0
        ol.focus()

        self._update_preview(ol.highlighted)
        enabled_count = sum(1 for n in names if n not in disabled)
        hint.update(
            Text(
                f"{enabled_count}/{len(names)} enabled in {self._scope} · "
                "Space toggles on/off · takes effect next turn",
                style="dim",
            )
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "skills-list":
            self._update_preview(event.option_index)

    def on_select_changed(self, event: Select.Changed) -> None:
        """Switch the scope whose prefs the toggles write to, and re-mark the list."""
        if event.select.id == "skill-scope-toggle" and isinstance(event.value, str):
            self._scope = event.value
            self._reload()

    def action_toggle(self) -> None:
        """Flip the highlighted skill on/off in the selected scope, then reload."""
        ol = self.query_one("#skills-list", OptionList)
        idx = ol.highlighted
        names = self.app._get_skill_names()
        if idx is None or not names or not (0 <= idx < len(names)):
            return
        name = names[idx]

        from novacode_cli.skills import skills_prefs

        disabled = skills_prefs.load_disabled(skills_prefs.scope_path(self._scope))
        currently_on = name not in disabled
        try:
            skills_prefs.set_skill_enabled(name, enabled=not currently_on, scope=self._scope)
        except ValueError as e:  # no project for project scope — shouldn't happen via UI
            self.query_one("#skills-hint", Static).update(Text(str(e), style="red"))
            return
        self._reload()
        # Reflect the new enabled count in the main status bar at once — the tick
        # loop doesn't refresh while idle, so push it here.
        self.app._skill_count_cache = None
        self.app._status_tail = None
        self.app._refresh_status()

    def _update_preview(self, idx: int | None) -> None:
        preview = self.query_one("#skill-detail-preview", Static)
        names = self.app._get_skill_names()
        if idx is None or not names or not (0 <= idx < len(names)):
            preview.update("")
            return

        skill_name = names[idx]

        from pathlib import Path
        from novacode_cli.config.config import Settings, settings

        skill_path = None
        scope = "unknown"

        search_dirs = []
        try:
            search_dirs.append((settings.ensure_user_skills_dir(), "global"))
        except Exception:
            pass
        try:
            claude_dir = Settings.get_global_claude_skills_dir()
            if claude_dir.exists():
                search_dirs.append((claude_dir, "global"))
        except Exception:
            pass
        try:
            for d in settings.get_project_skills_dirs():
                search_dirs.append((Path(d), "project"))
        except Exception:
            pass
        try:
            from novacode_cli.plugins.claude_plugins import plugin_skill_dirs

            for _pname, pd in plugin_skill_dirs():
                search_dirs.append((Path(pd), "plugin"))
        except Exception:
            pass

        for d, sc in search_dirs:
            if d and (d / skill_name / "SKILL.md").exists():
                skill_path = d / skill_name / "SKILL.md"
                scope = sc
                break

        desc = ""
        instructions = ""
        if skill_path:
            try:
                content = skill_path.read_text(encoding="utf-8")
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        instructions = parts[2].strip()
                        for line in parts[1].splitlines():
                            if line.strip().startswith("description:"):
                                desc = line.split(":", 1)[1].strip()
                else:
                    instructions = content.strip()
            except Exception as e:
                instructions = f"(error reading skill: {e})"
        else:
            instructions = "(SKILL.md file not found)"

        preview_text = Text()
        preview_text.append(f"Name: ", style="bold")
        preview_text.append(f"{skill_name}\n", style="bold #e0af68")
        preview_text.append(f"Scope: ", style="bold")
        preview_text.append(
            f"{scope}\n", style="bold yellow" if scope == "project" else "bold blue"
        )
        if desc:
            preview_text.append(f"Description: ", style="bold")
            preview_text.append(f"{desc}\n\n", style="dim")
        preview_text.append(f"Instructions / Content:\n", style="bold")
        preview_text.append(
            instructions[:1000] + "..." if len(instructions) > 1000 else instructions,
            style="italic dim",
        )

        preview.update(preview_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "toggle":
            self.action_toggle()
        elif event.button.id == "create":
            if not self._generating:
                self.app.run_worker(self._create_skill(), group="create_skill", exclusive=True)

    async def _create_skill(self) -> None:
        result = await self.app.push_screen_wait(SkillCreateModal())
        if result is not None:
            name = result["name"]
            desc = result["description"]
            scope = result["scope"]

            from novacode_cli.config.config import settings

            if scope == "project":
                base_dir = settings.ensure_project_skills_dir()
            else:
                base_dir = settings.ensure_user_skills_dir(self.app.assistant_id)

            skill_dir = base_dir / name
            if skill_dir.exists():
                if self.is_mounted:
                    try:
                        hint = self.query_one("#skills-hint", Static)
                        hint.update(Text(f"Skill '{name}' already exists.", style="yellow"))
                    except Exception:
                        pass
                self.app._log(Text(f"Skill '{name}' already exists.", style="yellow"))
                return

            self._generating = True
            if self.is_mounted:
                try:
                    self.query_one("#create", Button).disabled = True
                except Exception:
                    pass

                try:
                    hint = self.query_one("#skills-hint", Static)
                    hint.update(
                        Text(
                            f"Generating skill '{name}' using AI... Please wait.", style="bold cyan"
                        )
                    )
                except Exception:
                    pass
            self.app._log(Text(f"Generating skill '{name}' using AI...", style="cyan"))

            try:
                skill_dir.mkdir(parents=True, exist_ok=True)

                from novacode_cli.skills.skill_creation import _generate_skill

                content = await _generate_skill(
                    name,
                    base_dir=base_dir,
                    description=desc if desc else None,
                )

                if content is None:
                    raise RuntimeError("AI generation returned empty response.")

                if self.is_mounted:
                    try:
                        hint = self.query_one("#skills-hint", Static)
                        hint.update(Text(f"✓ Skill '{name}' created successfully!", style="green"))
                    except Exception:
                        pass
                self.app._log(Text(f"✓ Skill '{name}' created successfully!", style="green"))
            except Exception as e:
                if self.is_mounted:
                    try:
                        hint = self.query_one("#skills-hint", Static)
                        hint.update(Text(f"Failed to create skill: {e}", style="red"))
                    except Exception:
                        pass
                self.app._log(Text(f"Failed to create skill: {e}", style="red"))

            self._generating = False
            if self.is_mounted:
                try:
                    self.query_one("#create", Button).disabled = False
                except Exception:
                    pass
                self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class WikiScreen(ModalScreen[None]):
    """Native wiki manager: browse synthesized wiki pages, view details, ingest raw sources."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._pages: list[tuple[str, str, str]] = []  # list of (topic, path, summary)
        self._sources: list[str] = []  # list of raw source paths
        self._active_tab = "pages"  # "pages" or "inbox"

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Obsidian LLM Wiki Browser", style="bold"), id="modal-title")

            with Horizontal(id="wiki-tab-buttons"):
                yield Button("Synthesized Pages", id="tab-pages", variant="primary")
                yield Button("Web Clipper Inbox", id="tab-inbox")

            with Vertical(id="pages-container"):
                yield Static(Text("Synthesized Pages:", style="bold cyan"), id="pages-header")
                yield OptionList(id="wiki-pages-list")

            with Vertical(id="inbox-container"):
                yield Static(Text("Clipper Inbox / raw:", style="bold cyan"), id="inbox-header")
                yield OptionList(id="wiki-inbox-list")

            yield Static(Text("Preview:", style="bold yellow"), id="wiki-detail-header")
            yield Static("", id="wiki-detail-preview", classes="preview-box")
            yield Static("", id="wiki-hint")

            with Horizontal(id="modal-buttons"):
                yield Button("Ask About Page", id="ask-btn", variant="primary")
                yield Button("Ingest Selected", id="ingest-btn", variant="primary")
                yield Button("Refresh", id="refresh-btn")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()
        self._switch_tab("pages")

    def _reload(self) -> None:
        from novacode_cli.wiki.manager import WikiManager
        from novacode_cli.wiki.ingest import IngestEngine

        # Synthesized Pages
        try:
            mgr = WikiManager()
            mgr.ensure_structure()
            entries = mgr.read_index()
            self._pages = sorted(
                [(topic, info["path"], info.get("summary", "")) for topic, info in entries.items()],
                key=lambda x: x[0].lower(),
            )
        except Exception:
            self._pages = []

        # Clipper Inbox / raw sources
        try:
            engine = IngestEngine()
            self._sources = engine.list_raw_sources()
        except Exception:
            self._sources = []

        # Update lists
        ol_pages = self.query_one("#wiki-pages-list", OptionList)
        ol_pages.clear_options()
        for topic, path, summary in self._pages:
            label = Text.assemble(
                (f"{topic} ", "bold #7bb6ec"),
                (f"({path}) ", "dim"),
                (f"— {summary}" if summary else "", "dim"),
            )
            ol_pages.add_option(Option(label))
        if self._pages:
            ol_pages.highlighted = 0

        ol_inbox = self.query_one("#wiki-inbox-list", OptionList)
        ol_inbox.clear_options()
        for s in self._sources:
            label = Text(s, style="bold #a6e3a1")
            ol_inbox.add_option(Option(label))
        if self._sources:
            ol_inbox.highlighted = 0

        # Reset preview
        self._update_preview()

    def _switch_tab(self, tab: str) -> None:
        self._active_tab = tab
        if tab == "pages":
            self.query_one("#tab-pages", Button).variant = "primary"
            self.query_one("#tab-inbox", Button).variant = "default"
            self.query_one("#pages-container").display = True
            self.query_one("#inbox-container").display = False
            self.query_one("#ask-btn", Button).display = True
            self.query_one("#ingest-btn", Button).display = False
            try:
                self.query_one("#wiki-pages-list", OptionList).focus()
            except Exception:
                pass
        else:
            self.query_one("#tab-pages", Button).variant = "default"
            self.query_one("#tab-inbox", Button).variant = "primary"
            self.query_one("#pages-container").display = False
            self.query_one("#inbox-container").display = True
            self.query_one("#ask-btn", Button).display = False
            self.query_one("#ingest-btn", Button).display = True
            try:
                self.query_one("#wiki-inbox-list", OptionList).focus()
            except Exception:
                pass

        self._update_preview()

    def _update_preview(self) -> None:
        preview = self.query_one("#wiki-detail-preview", Static)
        hint = self.query_one("#wiki-hint", Static)

        if self._active_tab == "pages":
            ol = self.query_one("#wiki-pages-list", OptionList)
            idx = ol.highlighted
            if idx is None and self._pages:
                idx = 0
            if idx is not None and 0 <= idx < len(self._pages):
                topic, path, summary = self._pages[idx]
                from novacode_cli.wiki.manager import WikiManager

                try:
                    mgr = WikiManager()
                    content = mgr.read_page(path)
                    if content:
                        preview.update(Text(content))
                    else:
                        preview.update(Text("Could not load page content.", style="red"))
                except Exception as ex:
                    preview.update(Text(f"Error loading page: {ex}", style="red"))
                hint.update(Text("Press Ask About Page to query this page.", style="dim"))
            else:
                preview.update(Text("Select a page from the list to preview.", style="dim"))
                hint.update(Text(""))
        else:
            ol = self.query_one("#wiki-inbox-list", OptionList)
            idx = ol.highlighted
            if idx is None and self._sources:
                idx = 0
            if idx is not None and 0 <= idx < len(self._sources):
                source_path = self._sources[idx]
                from novacode_cli.wiki.ingest import IngestEngine

                try:
                    engine = IngestEngine()
                    source_full = engine.resolve_source(source_path)
                    content = source_full.read_text(encoding="utf-8")
                    preview.update(Text(content))
                except Exception as ex:
                    preview.update(Text(f"Error loading source: {ex}", style="red"))
                hint.update(Text("Press Ingest Selected to parse this source.", style="dim"))
            else:
                preview.update(Text("Select a source file to preview.", style="dim"))
                hint.update(Text(""))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._update_preview()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self._active_tab == "pages":
            self._ask_about_selected()
        else:
            self._ingest_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "tab-pages":
            self._switch_tab("pages")
        elif event.button.id == "tab-inbox":
            self._switch_tab("inbox")
        elif event.button.id == "refresh-btn":
            self._reload()
        elif event.button.id == "ingest-btn":
            self._ingest_selected()
        elif event.button.id == "ask-btn":
            self._ask_about_selected()

    def _ingest_selected(self) -> None:
        ol = self.query_one("#wiki-inbox-list", OptionList)
        if ol.highlighted is not None and 0 <= ol.highlighted < len(self._sources):
            source_path = self._sources[ol.highlighted]
            self.dismiss(None)
            self.app._dispatch(f"/ingest {source_path}")

    def _ask_about_selected(self) -> None:
        ol = self.query_one("#wiki-pages-list", OptionList)
        if ol.highlighted is not None and 0 <= ol.highlighted < len(self._pages):
            topic, path, summary = self._pages[ol.highlighted]
            self.dismiss(None)
            inp = self.app.query_one("#prompt", Input)
            inp.value = f"/ask regarding [[{topic}]]: "
            inp.cursor_position = len(inp.value)
            inp.focus()

    def action_close(self) -> None:
        self.dismiss(None)


class HookCreateModal(ModalScreen[dict | None]):
    """Modal dialog to collect inputs for creating a new hook."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Add New Hook", style="bold"), id="modal-title")
            yield Static(
                Text("Command to execute (e.g. python script.py):", style="bold"),
                id="hook-command-label",
            )
            yield Input(placeholder="e.g. python /path/to/script.py", id="hook-command")

            yield Static(
                Text("Events to subscribe to (comma-separated, blank for all):", style="bold"),
                id="hook-events-label",
            )
            yield Input(placeholder="e.g. session.start, tool.call", id="hook-events")
            yield Static(
                "Valid events: session.start, session.end, session.save, session.continue, "
                "model.switch, tool.call, tool.result, agent.message, user.message, error, "
                "remote.message, context.warning, compact, init.complete, notification",
                classes="modal-hint",
                id="events-help-text",
            )
            yield Static("", id="hook-create-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="do-save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self.query_one("#hook-command", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "do-save":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "hook-command":
            self.query_one("#hook-events", Input).focus()
        elif event.input.id == "hook-events":
            self._submit()

    def _submit(self) -> None:
        hint = self.query_one("#hook-create-hint", Static)
        try:
            cmd_str = self.query_one("#hook-command", Input).value.strip()
        except Exception:
            cmd_str = ""
        if not cmd_str:
            hint.update(Text("Command is required", style="red"))
            return

        # Check shell metacharacters
        _SHELL_METACHARACTERS = set("`$|;&")
        for c in _SHELL_METACHARACTERS:
            if c in cmd_str:
                hint.update(Text(f"Shell metacharacter '{c}' not allowed in command", style="red"))
                return

        try:
            events_str = self.query_one("#hook-events", Input).value.strip()
        except Exception:
            events_str = ""
        events = []
        if events_str:
            events = [e.strip() for e in events_str.split(",") if e.strip()]
            # Validate events
            from novacode_cli.hooks import HookEvent

            valid_events = {
                getattr(HookEvent, attr)
                for attr in dir(HookEvent)
                if not attr.startswith("_") and isinstance(getattr(HookEvent, attr), str)
            }
            valid_events.add("notification")
            invalid_events = [e for e in events if e not in valid_events]
            if invalid_events:
                hint.update(Text(f"Invalid events: {', '.join(invalid_events)}", style="red"))
                return

        self.dismiss({"command": cmd_str.split(), "events": events, "enabled": True})

    def action_cancel(self) -> None:
        self.dismiss(None)


class HooksScreen(ModalScreen[None]):
    """Native hooks manager: list, toggle, remove, test, add and reload hooks."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._hooks: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Hook Management", style="bold"), id="modal-title")
            yield Static(Text("Configured Hooks:", style="bold cyan"), id="hooks-section")
            yield OptionList(id="hooks-list")
            yield Static(Text("Hook Details:", style="bold yellow"), id="hook-detail-header")
            yield Static("", id="hook-detail-preview", classes="preview-box")
            yield Static("", id="hooks-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Add Hook", id="add", variant="primary")
                yield Button("Toggle Enable", id="toggle", variant="default")
                yield Button("Test Hook", id="test", variant="default")
                yield Button("Remove", id="remove", variant="error")
                yield Button("Reload", id="reload", variant="default")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    def _reload(self) -> None:
        from novacode_cli.hooks import _load_hooks

        try:
            self._hooks = _load_hooks()
        except Exception:
            self._hooks = []

        ol = self.query_one("#hooks-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        hint = self.query_one("#hooks-hint", Static)

        if not self._hooks:
            ol.add_option(Option("(no hooks configured)"))
            self.query_one("#hook-detail-preview", Static).update(
                Text("No hooks are currently configured.", style="dim")
            )
            self.query_one("#toggle", Button).disabled = True
            self.query_one("#test", Button).disabled = True
            self.query_one("#remove", Button).disabled = True
            hint.update(Text("Create a hook with the Add Hook button", style="dim"))
            return

        self.query_one("#toggle", Button).disabled = False
        self.query_one("#test", Button).disabled = False
        self.query_one("#remove", Button).disabled = False

        for h in self._hooks:
            command = " ".join(h.get("command", []))
            events = ", ".join(h.get("events", ["<all>"]))
            enabled = h.get("enabled", True)
            status = "✓" if enabled else "✗"
            status_style = "bold green" if enabled else "bold red"
            label = Text.assemble(
                (f"{status} ", status_style),
                (f"{command} ", "bold #73daca"),
                (f"· {events}", "dim"),
            )
            ol.add_option(Option(label))

        if keep is not None and 0 <= keep < len(self._hooks):
            ol.highlighted = keep
        else:
            ol.highlighted = 0
        ol.focus()

        self._update_preview(ol.highlighted)
        hint.update(
            Text("Select a hook to see details · Add/Toggle/Test/Remove hooks", style="dim")
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "hooks-list":
            self._update_preview(event.option_index)

    def _update_preview(self, idx: int | None) -> None:
        preview = self.query_one("#hook-detail-preview", Static)
        if idx is None or not self._hooks or not (0 <= idx < len(self._hooks)):
            preview.update("")
            return

        h = self._hooks[idx]
        command = " ".join(h.get("command", []))
        events = ", ".join(h.get("events", ["<all>"]))
        enabled = h.get("enabled", True)

        preview_text = Text()
        preview_text.append("Command: ", style="bold")
        preview_text.append(f"{command}\n", style="bold #73daca")
        preview_text.append("Events: ", style="bold")
        preview_text.append(f"{events}\n", style="cyan")
        preview_text.append("Status: ", style="bold")
        preview_text.append(
            "Enabled\n" if enabled else "Disabled\n", style="bold green" if enabled else "bold red"
        )

        preview.update(preview_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "add":
            self._add_hook()
        elif event.button.id == "toggle":
            self._toggle_hook()
        elif event.button.id == "test":
            self._test_hook()
        elif event.button.id == "remove":
            self._remove_hook()
        elif event.button.id == "reload":
            self._reload_hooks()

    @work
    async def _add_hook(self) -> None:
        result = await self.app.push_screen_wait(HookCreateModal())
        if result is not None:
            from novacode_cli.commands.hooks_handler import _save_hooks

            self._hooks.append(result)
            ok = _save_hooks(self._hooks)
            self.app._log(
                Text(
                    f"✓ Hook added: {' '.join(result['command'])}"
                    if ok
                    else "Failed to save hook configuration",
                    style="green" if ok else "red",
                )
            )
            self._reload()

    @work
    async def _toggle_hook(self) -> None:
        ol = self.query_one("#hooks-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._hooks):
            h = self._hooks[idx]
            from novacode_cli.commands.hooks_handler import _save_hooks

            h["enabled"] = not h.get("enabled", True)
            ok = _save_hooks(self._hooks)
            action = "enabled" if h["enabled"] else "disabled"
            self.app._log(
                Text(
                    f"✓ Hook {action}: {' '.join(h['command'])}"
                    if ok
                    else "Failed to save hook configuration",
                    style="green" if ok else "red",
                )
            )
            self._reload()

    @work
    async def _test_hook(self) -> None:
        ol = self.query_one("#hooks-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._hooks):
            h = self._hooks[idx]
            command = h.get("command", [])
            self.app._log(Text(f"Testing hook: {' '.join(command)}...", style="cyan"))
            import time
            from novacode_cli.hooks import dispatch_hook

            test_payload = {
                "test": True,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "message": "This is a test event",
            }
            try:
                await dispatch_hook("test", test_payload)
                self.app._log(
                    Text("✓ Test event fired successfully! Check hook logs.", style="green")
                )
            except Exception as e:
                self.app._log(Text(f"✗ Test failed: {e}", style="red"))

    @work
    async def _remove_hook(self) -> None:
        ol = self.query_one("#hooks-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._hooks):
            h = self._hooks[idx]
            ok = await self.app.push_screen_wait(
                ConfirmModal(
                    f"Remove hook: {' '.join(h.get('command', []))}?",
                    Text("This will remove the hook from configuration. This cannot be undone."),
                )
            )
            if ok:
                from novacode_cli.commands.hooks_handler import _save_hooks

                removed = self._hooks.pop(idx)
                saved = _save_hooks(self._hooks)
                self.app._log(
                    Text(
                        f"✓ Removed hook: {' '.join(removed.get('command', []))}"
                        if saved
                        else "Failed to save hook configuration",
                        style="green" if saved else "red",
                    )
                )
                self._reload()

    def _reload_hooks(self) -> None:
        from novacode_cli.hooks import reload_hooks

        reload_hooks()
        self.app._log(Text("✓ Hooks configuration reloaded from disk", style="green"))
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class ServersScreen(ModalScreen[None]):
    """Native dev servers manager: list servers, open in browser, stop them."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._servers: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Dev Server Management", style="bold"), id="modal-title")
            yield Static(Text("Running Dev Servers:", style="bold cyan"), id="servers-section")
            yield OptionList(id="servers-list")
            yield Static(Text("Server Details:", style="bold yellow"), id="server-detail-header")
            yield Static("", id="server-detail-preview", classes="preview-box")
            yield Static("", id="servers-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Open in Browser", id="open-browser", variant="primary")
                yield Button("Stop Server", id="stop-server", variant="error")
                yield Button("Stop All Managed", id="stop-all", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._reload()

    def _reload(self) -> None:
        from novacode_cli.server_runner.dev_server import list_servers

        try:
            self._servers = list_servers(include_external=True)
        except Exception:
            self._servers = []

        ol = self.query_one("#servers-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        hint = self.query_one("#servers-hint", Static)

        if not self._servers:
            ol.add_option(Option("(no dev servers running)"))
            self.query_one("#server-detail-preview", Static).update(
                Text("No dev servers are currently running.", style="dim")
            )
            self.query_one("#open-browser", Button).disabled = True
            self.query_one("#stop-server", Button).disabled = True
            self.query_one("#stop-all", Button).disabled = True
            hint.update(Text("Start a dev server via start_dev_server tool", style="dim"))
            return

        self.query_one("#open-browser", Button).disabled = False
        self.query_one("#stop-all", Button).disabled = False
        self.query_one("#stop-server", Button).disabled = False

        for s in self._servers:
            ext = s.pid == 0 and "external" in s.name
            status_style = "bold green" if s.status.value == "healthy" else "bold yellow"
            pid_label = f"external" if ext else f"PID {s.pid}"
            label = Text.assemble(
                (f"{s.name} ", "bold #73daca"),
                (f" · {pid_label} · ", "dim"),
                (s.url, "cyan"),
                (f" · {s.status.value}", status_style),
            )
            ol.add_option(Option(label))

        if keep is not None and 0 <= keep < len(self._servers):
            ol.highlighted = keep
        else:
            ol.highlighted = 0
        ol.focus()

        self._update_preview(ol.highlighted)
        hint.update(Text("Select a server to view details or perform actions", style="dim"))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "servers-list":
            self._update_preview(event.option_index)

    def _update_preview(self, idx: int | None) -> None:
        preview = self.query_one("#server-detail-preview", Static)
        if idx is None or not self._servers or not (0 <= idx < len(self._servers)):
            preview.update("")
            return

        s = self._servers[idx]
        ext = s.pid == 0 and "external" in s.name

        preview_text = Text()
        preview_text.append("Name: ", style="bold")
        preview_text.append(f"{s.name}\n", style="bold #73daca")
        preview_text.append("URL: ", style="bold")
        preview_text.append(f"{s.url}\n", style="cyan")
        preview_text.append("Status: ", style="bold")
        status_style = "bold green" if s.status.value == "healthy" else "bold yellow"
        preview_text.append(f"{s.status.value}\n", style=status_style)
        preview_text.append("PID: ", style="bold")
        preview_text.append(f"{'external' if ext else s.pid}\n", style="dim")
        preview_text.append("Command:\n", style="bold")
        preview_text.append(
            s.command if s.command else "(external server, command unknown)", style="italic dim"
        )

        preview.update(preview_text)
        # Disable stop server button for external servers
        self.query_one("#stop-server", Button).disabled = ext

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "open-browser":
            self._open_in_browser()
        elif event.button.id == "stop-server":
            self._stop_server()
        elif event.button.id == "stop-all":
            self._stop_all()

    def _open_in_browser(self) -> None:
        ol = self.query_one("#servers-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._servers):
            import webbrowser

            webbrowser.open(self._servers[idx].url)
            self.app._log(Text(f"✓ Opened {self._servers[idx].url}", style="green"))

    @work
    async def _stop_server(self) -> None:
        ol = self.query_one("#servers-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._servers):
            s = self._servers[idx]
            if s.pid == 0 and "external" in s.name:
                return
            from novacode_cli.server_runner.dev_server import stop_server

            ok = await stop_server(pid=s.pid)
            self.app._log(
                Text(
                    f"✓ Stopped '{s.name}' (PID {s.pid})"
                    if ok
                    else f"Failed to stop server '{s.name}'",
                    style="green" if ok else "red",
                )
            )
            self._reload()

    @work
    async def _stop_all(self) -> None:
        from novacode_cli.process_manager import ProcessManager

        count = await ProcessManager.get_instance().stop_all()
        self.app._log(
            Text(
                f"✓ Stopped {count} managed server(s)" if count else "No managed servers to stop",
                style="green" if count else "yellow",
            )
        )
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class RemoteScreen(ModalScreen[None]):
    """Native /remote screen: bridge status + start/stop/test, rendered as TUI
    components (the underlying logic is reused, but its output stays in-modal)."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        session_state,
        sandbox_id: str | None = None,
        sandbox_type: str | None = None,
    ) -> None:
        super().__init__()
        self._ss = session_state
        self._sandbox_id = sandbox_id
        self._sandbox_type = sandbox_type

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(
                Text("🔗 Remote Bridges Status & Config", style="bold cyan"), id="modal-title"
            )
            with VerticalScroll(id="remote-status-container"):
                yield Static("", id="remote-status")
            yield Static(Text("Start / Connect Bridge", style="bold"), id="remote-section-title")
            yield Input(
                placeholder="Bot token (blank = use saved config)",
                password=True,
                id="remote-token",
            )
            yield Input(placeholder="Channel/Chat ID (blank = saved/auto)", id="remote-chat")
            with Horizontal(id="modal-buttons"):
                yield Button("Start Discord", id="start-discord", variant="success")
                yield Button("Start Telegram", id="start-telegram", variant="success")
                yield Button("Test", id="test")
                yield Button("Stop All", id="stop", variant="error")
                yield Button("Close", id="close")
            yield Static("", id="remote-result")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self._refresh()

    def _refresh(self) -> None:
        from novacode_cli.remote.config import load_remote_config

        mgr = getattr(self._ss, "_remote_bridge_manager", None)
        bridges = mgr.active_bridges if mgr is not None else []
        t = Text()
        if bridges:
            t.append("⚡ Active Bridges:\n", style="bold cyan")
            for b in bridges:
                status = b.get("status", "")
                icon = (
                    "🟢" if status == "running" else ("🔴" if "error" in status.lower() else "🟡")
                )
                bot = f" (@{b['bot_user']})" if b.get("bot_user") else ""
                platform = str(b["platform"]).capitalize()

                t.append("  ")
                t.append(icon)
                t.append(f" {platform}{bot}", style="bold white")
                t.append(" — Chat: ")
                t.append(str(b["chat_id"]), style="yellow")
                t.append(" — Status: ")
                status_style = (
                    "bold green"
                    if status == "running"
                    else ("bold red" if "error" in status.lower() else "bold yellow")
                )
                t.append(status, style=status_style)
                t.append("\n")
        else:
            t.append("📭 No bridges active.\n", style="dim italic")
        try:
            saved = load_remote_config()
        except Exception:  # noqa: BLE001
            saved = {}
        if saved:
            t.append("\n💾 Saved Configurations:\n", style="bold magenta")
            if "discord" in saved:
                d = saved["discord"]
                tok = "✓ Configured" if d.get("token") else "✗ Missing Token"
                tok_style = "green" if d.get("token") else "red"
                t.append("  Discord", style="bold #5865F2")  # Discord brand color
                t.append(" · Token: ")
                t.append(tok, style=tok_style)
                t.append(" · Channel: ")
                t.append(str(d.get("channel_id", "—")), style="yellow")
                t.append("\n")
            if "telegram" in saved:
                tg = saved["telegram"]
                tok = "✓ Configured" if tg.get("token") else "✗ Missing Token"
                tok_style = "green" if tg.get("token") else "red"
                t.append("  Telegram", style="bold #24A1DE")  # Telegram brand color
                t.append(" · Token: ")
                t.append(tok, style=tok_style)
                t.append(" · Chat: ")
                t.append(str(tg.get("chat_id", "—")), style="yellow")
                t.append("\n")
        self.query_one("#remote-status", Static).update(t)

    def _build_args(self, platform: str) -> str:
        token = self.query_one("#remote-token", Input).value.strip()
        chat = self.query_one("#remote-chat", Input).value.strip()
        args = f"start {platform}"
        if token:
            args += f" --token {token}"
        if chat:
            args += f" --channel {chat}" if platform == "discord" else f" --chat {chat}"
        return args

    @work
    async def _run_remote(self, args: str) -> None:
        from novacode_cli.commands.commands import handle_command
        from novacode_cli.config.config import console as _c

        self.query_one("#remote-result", Static).update(Text("Working…", style="dim"))
        app = self.app
        try:
            with _c.capture() as cap:
                await handle_command(
                    f"/remote {args}",
                    app.agent,  # type: ignore
                    app.token_tracker,  # type: ignore
                    self._ss,
                    app.assistant_id,  # type: ignore
                    model_name=app.model_name,  # type: ignore
                    image_tracker=app.image_tracker,  # type: ignore
                    sandbox_id=self._sandbox_id,
                    sandbox_type=self._sandbox_type,
                )
            out = cap.get().strip()
        except Exception as ex:  # noqa: BLE001
            out = f"Error: {ex}"
        # Native one-line result: take the last meaningful line, color by outcome.
        plain = Text.from_ansi(out).plain.strip()
        last = next(
            (ln.strip() for ln in reversed(plain.splitlines()) if ln.strip()),
            "Done",
        )
        errored = any(x in plain for x in ("❌", "✗", "Error", "error", "No "))
        self.query_one("#remote-result", Static).update(
            Text(last, style="red" if errored else "green")
        )
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.dismiss(None)
        elif bid == "start-discord":
            self._run_remote(self._build_args("discord"))
        elif bid == "start-telegram":
            self._run_remote(self._build_args("telegram"))
        elif bid == "test":
            self._run_remote("test")
        elif bid == "stop":
            self._run_remote("stop")

    def action_close(self) -> None:
        self.dismiss(None)


class RalphScreen(ModalScreen[None]):
    """Native /ralph modal: a live dashboard for Ralph autonomous runs.

    Shows a header with task info, a scrollable list of iteration cards
    that update in real time, action buttons (Stop / Checkpoint / Close),
    and a summary when the run finishes.  Background ``--status`` is also
    rendered inside this modal instead of inline in the transcript.
    """

    BINDINGS = [("escape", "close", "Close")]

    _ITER_GLYPH = {
        "running": ("▶", "yellow"),
        "done": ("✓", "green"),
        "failed": ("✗", "red"),
    }

    DEFAULT_CSS = """
    RalphScreen {
        align: center middle;
    }
    RalphScreen #modal-box {
        width: 80%; max-width: 110; height: auto; max-height: 90%;
        border: thick $accent; background: $surface;
        padding: 1 4; layer: overlay;
    }
    RalphScreen #ralph-header {
        margin-bottom: 1; padding: 0 0;
    }
    RalphScreen #ralph-iters {
        height: auto; max-height: 60%;
        padding: 0 1;
    }
    RalphScreen .ralph-iter-card {
        height: auto; padding: 0 1; margin: 0 0;
        border-left: thick $accent;
    }
    RalphScreen .ralph-iter-card.done {
        border-left: thick $success;
    }
    RalphScreen .ralph-iter-card.failed {
        border-left: thick $error;
    }
    RalphScreen .ralph-iter-card.running {
        border-left: thick $warning;
    }
    RalphScreen #ralph-summary {
        margin-top: 1; padding: 0 0;
    }
    RalphScreen #ralph-status-table {
        margin-top: 1; padding: 0 0;
    }
    RalphScreen #modal-buttons {
        height: auto; align: center middle;
        margin-top: 1; padding: 0 0;
    }
    RalphScreen #modal-buttons Button { margin: 0 1; }
    """

    def __init__(
        self,
        session_state: Any,
        agent: Any,
        assistant_id: str,
        token_tracker: Any,
        args: str,
        execute_fn: Any,
    ) -> None:
        super().__init__()
        self._ss = session_state
        self._agent = agent
        self._assistant_id = assistant_id
        self._token_tracker = token_tracker
        self._args = args
        self._execute_fn = execute_fn
        self._iter_cards: dict[int, Static] = {}
        self._finished = False
        self._stop_requested = False

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(
                Text("🔁 Ralph — Autonomous Mode", style="bold cyan"),
                id="modal-title",
            )
            yield Static("", id="ralph-header")
            with VerticalScroll(id="ralph-iters"):
                pass  # iteration cards mounted dynamically
            yield Static("", id="ralph-summary")
            yield Static("", id="ralph-status-table")
            with Horizontal(id="modal-buttons"):
                yield Button("⏹ Stop", id="ralph-stop", variant="error")
                yield Button("💾 Checkpoint", id="ralph-checkpoint", variant="warning")
                yield Button("Close", id="ralph-close")

    def on_mount(self) -> None:
        animate_modal_screen(self)
        self.query_one("#ralph-header", Static).update(
            Text("Starting Ralph run…", style="dim italic")
        )
        self.app.run_worker(self._run_ralph(), group="ralph", exclusive=True)

    # ── Event handlers ─────────────────────────────────────────────

    def _on_ralph_event(self, event: Any) -> None:
        """Drive modal widgets from structured Ralph events."""
        if not self.is_mounted:
            return
        from novacode_cli.commands import ralph_events as rev

        if isinstance(event, rev.RalphStarted):
            self._iter_cards.clear()
            iters = "unlimited" if event.max_iterations == 0 else str(event.max_iterations)
            title = "🔁 Ralph Mode (Resumed)" if event.resumed_from else "🔁 Ralph Mode"
            t = Text()
            t.append(f"{title}\n", style="bold")
            t.append("Task: ", style="bold")
            t.append(f"{event.task}\n")
            t.append("Max iterations: ", style="bold")
            t.append(f"{iters}\n")
            if event.resumed_from:
                t.append("Resuming from: ", style="bold")
                t.append(f"iteration {event.resumed_from}\n")
            t.append("Mode: ", style="bold")
            t.append("background (non-blocking)" if event.background else "foreground")
            self.query_one("#ralph-header", Static).update(t)

        elif isinstance(event, rev.IterationStarted):
            text = self._iter_text(event.iteration, event.max_iterations, "running")
            card = Static(text, classes="ralph-iter-card running")
            self._iter_cards[event.iteration] = card
            self.query_one("#ralph-iters", VerticalScroll).mount(card)
            card.scroll_visible()

        elif isinstance(event, rev.IterationFinished):
            status = "done" if event.ok else "failed"
            text = self._iter_text(
                event.iteration,
                event.max_iterations,
                status,
                event.elapsed,
                event.error,
            )
            card = self._iter_cards.get(event.iteration)
            if card is not None:
                try:
                    card.set_classes(f"ralph-iter-card {status}")
                    card.update(text)
                except Exception:  # noqa: BLE001
                    pass

        elif isinstance(event, rev.RalphFinished):
            self._finished = True
            t = Text()
            t.append("📊 Ralph finished", style="bold")
            t.append(f" — {event.completed} completed", style="green")
            if event.failed:
                t.append(f", {event.failed} failed", style="red")
            t.append(f" of {event.total} iteration(s)", style="dim")
            t.append(f"\nReason: {event.reason}", style="dim italic")
            self.query_one("#ralph-summary", Static).update(t)
            try:
                self.query_one("#ralph-stop", Button).disabled = True
                self.query_one("#ralph-checkpoint", Button).disabled = True
                self.query_one("#ralph-close", Button).disabled = False
            except Exception:  # noqa: BLE001
                pass

        elif isinstance(event, rev.StatusSnapshot):
            self._render_status(event)

    def _render_status(self, snap: Any) -> None:
        """Render ``--status`` snapshot as a native table inside the modal."""
        from rich.console import Group
        from rich.table import Table

        header = Text("Ralph Background Tasks\n", style="bold")
        if not snap.rows:
            header.append("No background Ralph tasks running.", style="dim")
            self.query_one("#ralph-status-table", Static).update(header)
            return

        table = Table(show_edge=False, pad_edge=False, expand=False)
        table.add_column("", width=2)
        table.add_column("Iter")
        table.add_column("Status")
        table.add_column("Elapsed", justify="right")
        table.add_column("Task")
        glyphs = {
            "running": ("⏳", "yellow"),
            "completed": ("✓", "green"),
            "failed": ("✗", "red"),
        }
        for row in snap.rows:
            g, color = glyphs.get(row.status, ("•", "dim"))
            disp = (
                f"{row.iteration}/{row.max_iterations}"
                if row.max_iterations > 0
                else str(row.iteration)
            )
            desc = row.task if len(row.task) <= 50 else row.task[:50] + "…"  # noqa: PLR2004
            table.add_row(
                Text(g, style=color),
                disp,
                Text(row.status, style=color),
                f"{row.elapsed:.0f}s",
                desc,
            )
        summary = Text()
        summary.append(f"\nTotal {snap.total}", style="dim")
        summary.append(f"  ·  running {snap.running}", style="yellow")
        summary.append(f"  ·  completed {snap.completed}", style="green")
        summary.append(f"  ·  failed {snap.failed}", style="red")
        self.query_one("#ralph-status-table", Static).update(Group(header, table, summary))

    def _iter_text(
        self,
        iteration: int,
        max_iterations: int,
        status: str,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> Text:
        """One iteration card, styled by status."""
        glyph, color = self._ITER_GLYPH.get(status, ("•", "dim"))
        disp = f"{iteration}/{max_iterations}" if max_iterations > 0 else str(iteration)
        t = Text()
        t.append(f"{glyph} Iteration {disp}", style=f"bold {color}")
        if status == "running":
            t.append("  — running…", style="dim")
        else:
            t.append(
                f"  — {'done' if status == 'done' else 'failed'}",
                style=color,
            )
            if elapsed is not None:
                t.append(f" ({elapsed:.1f}s)", style="dim")
            if error:
                t.append(f"\n    {error}", style="red")
        return t

    async def _run_ralph(self) -> None:
        """Kick off the Ralph handler with events routed to this modal."""
        import threading

        from novacode_cli.commands.ralph_handler import handle_ralph_command

        loop_tid = threading.get_ident()

        def _emit(message: str = "") -> None:
            """Thread-safe Rich-markup emitter — logs to main transcript."""
            try:
                renderable = Text.from_markup(message) if message else Text("")
            except Exception:  # noqa: BLE001
                renderable = Text(message)
            app = self.app
            if threading.get_ident() == loop_tid:
                app._log(renderable)  # noqa: SLF001
            else:
                try:
                    app.call_from_thread(app._log, renderable)  # noqa: SLF001
                except Exception:  # noqa: BLE001
                    pass

        def _on_event(event: Any) -> None:
            if threading.get_ident() == loop_tid:
                self._on_ralph_event(event)
            else:
                try:
                    self.app.call_from_thread(self._on_ralph_event, event)
                except Exception:  # noqa: BLE001
                    pass

        try:
            await handle_ralph_command(
                self._agent,
                self._ss,
                self._assistant_id,
                self._token_tracker,
                self._args or None,
                execute_fn=self._execute_fn,
                emit=_emit,
                on_event=_on_event,
            )
        except asyncio.CancelledError:
            self._finished = True
            if self.is_mounted:
                try:
                    self.query_one("#ralph-summary", Static).update(
                        Text("⏹ Ralph run stopped / cancelled by user.", style="yellow bold")
                    )
                    self.query_one("#ralph-stop", Button).disabled = True
                    self.query_one("#ralph-checkpoint", Button).disabled = True
                    self.query_one("#ralph-close", Button).disabled = False
                except Exception:  # noqa: BLE001
                    pass
        except Exception as ex:  # noqa: BLE001
            self._finished = True
            if self.is_mounted:
                try:
                    self.query_one("#ralph-summary", Static).update(
                        Text(f"Error: {ex}", style="red bold")
                    )
                    self.query_one("#ralph-stop", Button).disabled = True
                    self.query_one("#ralph-checkpoint", Button).disabled = True
                    self.query_one("#ralph-close", Button).disabled = False
                except Exception:  # noqa: BLE001
                    pass

    # ── Button handling ────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "ralph-close":
            self.dismiss(None)
        elif bid == "ralph-stop":
            self._ss._ralph_stop_requested = True  # noqa: SLF001
            self._stop_requested = True
            self.app.workers.cancel_group(self.app, "ralph")
            if self.is_mounted:
                try:
                    self.query_one("#ralph-summary", Static).update(
                        Text("⏹ Stop requested…", style="yellow bold")
                    )
                    self.query_one("#ralph-stop", Button).disabled = True
                    self.query_one("#ralph-checkpoint", Button).disabled = True
                except Exception:  # noqa: BLE001
                    pass
        elif bid == "ralph-checkpoint":
            self._ss._ralph_checkpoint_requested = True  # noqa: SLF001
            if self.is_mounted:
                try:
                    self.query_one("#ralph-summary", Static).update(
                        Text("💾 Checkpoint requested…", style="yellow")
                    )
                    self.query_one("#ralph-stop", Button).disabled = True
                    self.query_one("#ralph-checkpoint", Button).disabled = True
                except Exception:  # noqa: BLE001
                    pass

    def action_close(self) -> None:
        self.dismiss(None)




class BackgroundTasksScreen(ModalScreen[dict | None]):
    """The Background Tasks panel: live list + actions.

    Acts on the registry directly for terminate/restart/clear (keeps the panel
    open); dismisses with a result for copy/logs, which need the running app.
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("t", "terminate", "Terminate"),
        ("r", "restart", "Restart"),
        ("l", "logs", "Logs"),
        ("c", "copy", "Copy cmd"),
        ("x", "clear", "Clear done"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(Text("Background Tasks", style="bold"), id="modal-title")
            yield OptionList(id="tasks-list")
            yield Static(
                Text(
                    "↑/↓ select · [t]erminate · [r]estart · [l]ogs · [c]opy cmd · [x] clear done · Esc close",
                    style="dim",
                ),
                id="tasks-hint",
            )

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)  # live runtimes

    def _registry(self):
        from novacode_cli.shell.jobs import get_registry

        return get_registry()

    def _refresh(self) -> None:
        from novacode_cli.shell.jobs import fmt_runtime

        ol = self.query_one("#tasks-list", OptionList)
        keep = ol.highlighted
        self._tasks = self._registry().list_jobs()
        ol.clear_options()
        if not self._tasks:
            ol.add_option(Option("No background tasks."))
        else:
            style = {"running": "cyan", "done": "green", "failed": "red", "terminated": "yellow"}
            for t in self._tasks:
                line = Text()
                line.append(f"{t.status_glyph()} ", style=style.get(t.status, "white"))
                line.append(f"{t.task_id}  ", style="bold")
                line.append(f"{t.command[:44]}", style="white")
                state = t.status + (f" · exit {t.exit_code}" if t.exit_code is not None else "")
                line.append(f"\n   {state} · {fmt_runtime(t.runtime())}", style="dim")
                ol.add_option(Option(line))
        if self._tasks and keep is not None:
            ol.highlighted = min(keep, len(self._tasks) - 1)

    def _selected(self):
        ol = self.query_one("#tasks-list", OptionList)
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._tasks)):
            return None
        return self._tasks[idx]

    def action_terminate(self) -> None:
        t = self._selected()
        if t is not None:
            self._registry().terminate(t.id)
            self._refresh()

    def action_restart(self) -> None:
        t = self._selected()
        if t is not None:
            self._registry().restart(t.id)
            self._refresh()

    def action_clear(self) -> None:
        self._registry().clear_completed()
        self._refresh()

    def action_logs(self) -> None:
        t = self._selected()
        if t is not None:
            self.dismiss({"action": "logs", "task_id": t.task_id})

    def action_copy(self) -> None:
        t = self._selected()
        if t is not None:
            self.dismiss({"action": "copy", "command": t.command})

    def action_cancel(self) -> None:
        self.dismiss(None)
