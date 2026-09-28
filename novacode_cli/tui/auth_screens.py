"""Screens for managing stored provider and service credentials (``/auth``).

`AuthManagerScreen` lists every credential Nova can store, with a badge showing
where each one stands, and routes the user into `AuthPromptScreen`. The prompt
is the only place a credential is written, and the only place one is deleted
(after a confirmation) — the manager never writes, so there is one path to
audit.

Security notes:

- The key field is rendered with ``password=True`` so the value is never echoed
  to the terminal.
- The key value is never logged, never included in a notification, and never
  interpolated into an error message. Only the credential's *name* (its
  environment variable) is ever shown.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from novacode_cli.config.credentials import (
    CredentialMeta,
    CredentialStore,
    delete_credential,
    has_stored_credential,
    set_credential,
)
from novacode_cli.config.provider_auth import (
    ProviderAuthSource,
    ProviderAuthState,
    ProviderAuthStatus,
    credential_env_var,
    credential_names,
    get_all_auth_statuses,
    is_service,
    provider_display_name,
)
from novacode_cli.tui.animations import animate_modal_screen
from novacode_cli.tui.auth_display import format_auth_badge
from novacode_cli.tui.screens import ConfirmModal

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType


#: What a *service* credential unlocks, for the rows that need to say it. Only a
#: service that gates a tool earns an entry: the voice credentials already name
#: their own axis in the display name ("Deepgram (speech to text)"), so reusing
#: Tavily's "gates web search" note for them was simply wrong.
_SERVICE_NOTES: dict[str, str] = {
    "tavily": "gates web search",
}


def _endpoint_providers() -> frozenset[str]:
    """Return the providers that accept a custom endpoint.

    Imported lazily so this module does not pull `model_manager` (and through it
    the LangChain model classes) just to answer a gating question.
    """
    from novacode_cli.config.model_manager import ENDPOINT_PROVIDERS

    return ENDPOINT_PROVIDERS


def _is_http_url(value: str) -> bool:
    """Whether *value* looks like an http(s) URL.

    Args:
        value: Candidate endpoint URL.

    Returns:
        True when the scheme is http or https and a host is present.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


class AuthManagerScreen(ModalScreen[None]):
    """List stored credentials and route the user into the key prompt.

    Always dismisses with ``None``; the caller reads :attr:`saved` and
    :attr:`deleted` afterwards to report what changed. State lives in the
    credential store, so the list is re-read rather than patched after any
    change.

    Credential reads are filesystem/keychain work and run in a worker thread:
    a status pass touches the OS credential store once per provider, which is
    not something to do inside `compose`.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+d", "delete", "Delete the stored key"),
    ]

    def __init__(self, *, focus: str | None = None) -> None:
        """Initialize the manager.

        Args:
            focus: Credential name whose row should be highlighted once the
                list is built — used when reopening so the cursor lands on the
                row the user just worked on instead of resetting to the top.
        """
        super().__init__()
        self._focus = focus
        self._names: list[str] = []
        self._statuses: dict[str, ProviderAuthStatus] = {}
        self._metas: dict[str, CredentialMeta] = {}
        #: Credential names saved or deleted while this screen was open, in the
        #: order they happened. The caller reports them; the screen does not.
        self.saved: list[str] = []
        self.deleted: list[str] = []
        self.warnings: list[str] = []

    def compose(self) -> ComposeResult:
        """Compose the manager list and its buttons."""
        with Vertical(id="modal-box"):
            yield Static(Text("Manage API keys", style="bold"), id="modal-title")
            yield Static(
                Text(
                    "Stored in your OS credential store, so they survive a restart "
                    "and apply to every Nova session.\n"
                    "Providers that need no key (Ollama) are not listed.",
                    style="dim",
                ),
                id="auth-desc",
            )
            yield OptionList(id="auth-list")
            yield Static("", id="auth-hint")
            with Horizontal(id="modal-buttons"):
                yield Button("Add / Replace", id="edit", variant="primary")
                yield Button("Delete", id="delete", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        """Animate in and load the credential list."""
        animate_modal_screen(self)
        self.reload()

    @work(thread=True, group="auth_reload", exclusive=True)
    def reload(self) -> None:
        """Re-read credentials and repaint, off the UI thread."""
        try:
            statuses = get_all_auth_statuses(include_providers_without_keys=True)
        except Exception:  # noqa: BLE001 — a failed pass must still render a list
            statuses = {}
        # Suppressed, not handled: the screen is dismissed before the background
        # pass finishes whenever the user closes the manager quickly, and there
        # is nothing left to repaint by then.
        with contextlib.suppress(Exception):
            self.app.call_from_thread(self._paint, statuses)

    def _paint(self, statuses: dict[str, ProviderAuthStatus]) -> None:
        """Rebuild the option list from *statuses*.

        Named `_paint`, not `_render`: `Widget._render` is part of Textual's own
        render pipeline (it returns a Visual) and shadowing it made the screen
        raise on every frame.

        Args:
            statuses: Readiness per provider/service id.
        """
        self._statuses = statuses
        names = [name for name in credential_names() if name in statuses]
        self._names = names

        self._metas = CredentialStore().list_meta(
            env_var for env_var in (credential_env_var(name) for name in names) if env_var
        )

        option_list = self.query_one("#auth-list", OptionList)
        option_list.clear_options()
        for name in names:
            option_list.add_option(Option(self._row_label(name, statuses[name]), id=name))

        if not names:
            option_list.add_option(Option("No credentials can be stored here.", id="__none__"))

        if self._focus and self._focus in names:
            option_list.highlighted = names.index(self._focus)
        option_list.focus()
        self._update_hint()

    def _row_label(self, name: str, status: ProviderAuthStatus) -> Text:
        """Build one row: display name, badge, and a short status detail.

        Args:
            name: Provider or service id.
            status: Its readiness status.

        Returns:
            The composed row text.
        """
        display = provider_display_name(name)
        text = Text()
        text.append(display, style="bold")
        text.append("  ")
        text.append_text(format_auth_badge(status))
        detail = self._row_detail(name, status)
        if detail:
            text.append(f"  {detail}", style="dim")
        return text

    def _row_detail(self, name: str, status: ProviderAuthStatus) -> str:
        """Return the trailing description shown on a row.

        Args:
            name: Provider or service id.
            status: Its readiness status.

        Returns:
            A short detail string, or an empty string when the badge says enough.
        """
        if status.state is ProviderAuthState.CONFIGURED:
            env_var = credential_env_var(name)
            meta = self._metas.get(env_var) if env_var else None
            parts = []
            if meta is not None and meta.base_url:
                parts.append(meta.base_url)
            if is_service(name):
                note = _SERVICE_NOTES.get(name)
                if note:
                    parts.append(note)
            return "  ·  ".join(parts)
        return ""

    def _highlighted(self) -> str | None:
        """Return the highlighted credential name, if it is a real row."""
        option_list = self.query_one("#auth-list", OptionList)
        index = option_list.highlighted
        if index is None or not (0 <= index < len(self._names)):
            return None
        return self._names[index]

    def _update_hint(self) -> None:
        """Refresh the footer for the highlighted row."""
        name = self._highlighted()
        hint = self.query_one("#auth-hint", Static)
        if name is None:
            hint.update(Text("Esc close", style="dim"))
            return
        action = "replace" if self._stored(name) else "add"
        deletable = "  ·  Ctrl+D delete" if self._stored(name) else ""
        hint.update(Text(f"Enter {action} the key{deletable}  ·  Esc close", style="dim"))

    def _stored(self, name: str) -> bool:
        """Whether a credential for *name* came from the credential store.

        Args:
            name: Provider or service id.

        Returns:
            True when `/auth` can replace or delete it.
        """
        status = self._statuses.get(name)
        return status is not None and status.source is ProviderAuthSource.STORED

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Open the prompt for the selected credential."""
        if event.option_list.id == "auth-list":
            self.open_prompt()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Update the footer when the highlight moves."""
        if event.option_list.id == "auth-list":
            self._update_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the manager's buttons."""
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "edit":
            self.open_prompt()
        elif event.button.id == "delete":
            self.remove_selected()

    @work(group="auth_action", exclusive=True)
    async def open_prompt(self) -> None:
        """Push the key prompt for the highlighted credential and reload."""
        name = self._highlighted()
        if name is None:
            return
        env_var = credential_env_var(name)
        if env_var is None:
            return
        prompt = AuthPromptScreen(name, existing=self._metas.get(env_var))
        result = await self.app.push_screen_wait(prompt)
        if prompt.warnings:
            self.warnings.extend(prompt.warnings)
            for warning in prompt.warnings:
                # markup=False: the text interpolates an env var name and a path
                # step that may contain characters Textual's markup reads as tags.
                self.app.notify(warning, severity="warning", markup=False)
        if result:
            self.saved.append(name)
            self._focus = name
            self.reload()

    @work(group="auth_action", exclusive=True)
    async def remove_selected(self) -> None:
        """Confirm, then delete the highlighted credential."""
        name = self._highlighted()
        if name is None:
            return
        env_var = credential_env_var(name)
        if env_var is None:
            return
        if not self._stored(name) or not has_stored_credential(env_var):
            # Deleting an environment-supplied key is not something `/auth` can
            # do, and pretending otherwise would leave the user thinking it is
            # gone. Say so instead.
            self.app.notify(
                f"{env_var} is not stored here — it comes from your environment. "
                f"Unset it in your shell or .env instead.",
                severity="warning",
                markup=False,
            )
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete the stored key for {provider_display_name(name)}?",
                Text(f"This removes {env_var} from your credential store. It cannot be undone."),
            )
        )
        if not confirmed:
            return
        if delete_credential(env_var):
            self.deleted.append(name)
        else:
            self.app.notify(
                f"Could not delete the credential for {env_var}.",
                severity="error",
                markup=False,
            )
        self._focus = name
        self.reload()

    def action_close(self) -> None:
        """Dismiss the manager."""
        self.dismiss(None)

    def action_delete(self) -> None:
        """Delete the highlighted credential, after confirmation."""
        self.remove_selected()


class AuthPromptScreen(ModalScreen[str | None]):
    """Accept (or delete) the credential for one provider or service.

    Dismisses with the credential name when it was saved, or ``None`` when the
    user cancelled. Warnings from the write are left on :attr:`warnings` for the
    caller to surface: a keychain write that failed partially is the one signal
    the user must not miss, and a toast raised from inside a dismissing screen
    competes with its own teardown.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("f2", "toggle_advanced", "Advanced"),
        ("ctrl+d", "delete", "Delete key"),
    ]

    def __init__(self, name: str, *, existing: CredentialMeta | None = None) -> None:
        """Initialize the prompt.

        Args:
            name: Provider or service id whose credential is being set.
            existing: Metadata already recorded for this credential, used to
                prefill the endpoint so a rotation does not silently drop it.
        """
        super().__init__()
        self._name = name
        self._existing = existing
        self._advanced = False
        self._warnings: list[str] = []

    @property
    def warnings(self) -> list[str]:
        """Warnings produced by a save that nonetheless stored the key."""
        return list(self._warnings)

    @property
    def _env_var(self) -> str | None:
        """The environment variable this credential is filed under."""
        return credential_env_var(self._name)

    @property
    def _supports_endpoint(self) -> bool:
        """Whether this credential's provider accepts a custom endpoint."""
        return self._name in _endpoint_providers()

    def compose(self) -> ComposeResult:
        """Compose the key prompt, with the advanced panel hidden."""
        env_var = self._env_var or self._name
        with Vertical(id="modal-box"):
            yield Static(
                Text(f"{provider_display_name(self._name)} API key", style="bold"),
                id="modal-title",
            )
            yield Static(
                Text(
                    f"Paste the key below. It is stored in your OS credential store as "
                    f"{env_var} and applied to this session immediately.",
                    style="dim",
                ),
                id="auth-desc",
            )
            yield Input(placeholder="API key (input is hidden)", password=True, id="auth-key")
            yield Static(
                Text(
                    "Enter save  ·  F2 advanced  ·  Ctrl+D delete stored key  ·  Esc cancel",
                    style="dim",
                ),
                id="auth-hint",
            )
            yield Static("", id="auth-error")
            yield Static(
                Text("Endpoint URL (blank = provider default)", style="bold"),
                id="auth-endpoint-label",
            )
            yield Input(placeholder="https://…", id="auth-endpoint")

    def on_mount(self) -> None:
        """Hide the advanced panel and focus the key field."""
        animate_modal_screen(self)
        self._apply_advanced_visibility()
        self.query_one("#auth-key", Input).focus()

    def _apply_advanced_visibility(self) -> None:
        """Show the endpoint field only when advanced is open and it applies."""
        show = self._advanced and self._supports_endpoint
        self.query_one("#auth-endpoint-label", Static).display = show
        self.query_one("#auth-endpoint", Input).display = show

    def action_toggle_advanced(self) -> None:
        """Show or hide the endpoint field."""
        self._advanced = not self._advanced
        self._apply_advanced_visibility()
        if self._advanced and self._supports_endpoint:
            endpoint = self.query_one("#auth-endpoint", Input)
            if not endpoint.value and self._existing is not None and self._existing.base_url:
                # Prefill the stored endpoint so it is visible and editable
                # rather than silently still in effect.
                endpoint.value = self._existing.base_url
            endpoint.focus()
        else:
            self.query_one("#auth-key", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Save on Enter from either field."""
        event.stop()
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the prompt's buttons."""
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._save()

    def _show_error(self, message: str) -> None:
        """Render an error under the key field."""
        self.query_one("#auth-error", Static).update(Text(message, style="red"))

    def _save(self) -> None:
        """Validate and persist the credential."""
        env_var = self._env_var
        if env_var is None:
            self._show_error("This credential cannot be stored.")
            return

        key = self.query_one("#auth-key", Input).value.strip()
        if not key:
            self._show_error("API key cannot be empty.")
            return

        base_url = ""
        if self._supports_endpoint:
            base_url = self.query_one("#auth-endpoint", Input).value.strip()
            existing_url = (self._existing.base_url if self._existing else "") or ""
            # Validate only a *changed* value. The field is prefilled with the
            # stored endpoint, so a legacy non-http(s) value must not block
            # rotating just the key.
            if base_url and base_url != existing_url and not _is_http_url(base_url):
                self._show_error("Endpoint URL must be an http(s) URL.")
                return

        outcome = set_credential(env_var, key, base_url=base_url or None)
        if not outcome.ok:
            # The store reported the write failed; the message names the env var
            # only, never the value.
            self._warnings.extend(outcome.warnings)
            self._show_error(outcome.warnings[0] if outcome.warnings else "Could not save.")
            return

        self._warnings.extend(outcome.warnings)
        self.dismiss(self._name)

    @work(exclusive=True)
    async def action_delete(self) -> None:
        """Delete the stored credential after a confirmation."""
        env_var = self._env_var
        if env_var is None:
            return
        if not has_stored_credential(env_var):
            self._show_error("No stored key to delete for this credential.")
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete the stored key for {provider_display_name(self._name)}?",
                Text(f"This removes {env_var} from your credential store. It cannot be undone."),
            )
        )
        if not confirmed:
            return
        delete_credential(env_var)
        self.dismiss(self._name)

    def action_cancel(self) -> None:
        """Discard the prompt."""
        self.dismiss(None)
