"""Shared formatting for provider credential status.

Two surfaces show the same readiness facts and must not disagree about them:

- ``/auth`` renders a bracketed, styled **badge** per provider
  (``[stored]``, ``[env set: ANTHROPIC_API_KEY]``, ``[missing]``).
- ``/model`` renders a short **indicator** next to a provider's header row, and
  nothing at all when the provider is ready — a working provider needs no
  annotation.

Both live here so the wording and the styling are decided once. They are
deliberately different in shape: a badge is read while hunting for the key you
want to replace, an indicator is read while hunting for a model, where anything
longer than a couple of words competes with the model names.
"""

from __future__ import annotations

from rich.text import Text

from novacode_cli.config.provider_auth import (
    ProviderAuthSource,
    ProviderAuthState,
    ProviderAuthStatus,
)

_BADGE_STYLES: dict[ProviderAuthState, str] = {
    ProviderAuthState.CONFIGURED: "green",
    ProviderAuthState.MISSING: "bold yellow",
    ProviderAuthState.NOT_REQUIRED: "dim",
    ProviderAuthState.MANAGED: "dim",
    ProviderAuthState.UNKNOWN: "dim",
}

#: Short phrase per state. Configured providers map to "" because a working
#: provider needs no annotation, and an empty string keeps the callers free of a
#: special case.
_SHORT_STATES: dict[ProviderAuthState, str] = {
    ProviderAuthState.CONFIGURED: "",
    ProviderAuthState.MISSING: "no key",
    ProviderAuthState.NOT_REQUIRED: "no key required",
    ProviderAuthState.MANAGED: "custom auth",
    ProviderAuthState.UNKNOWN: "credentials unknown",
}


def format_auth_badge(status: ProviderAuthStatus) -> Text:
    """Format the ``/auth`` credential badge for a provider.

    Args:
        status: Provider or service readiness status.

    Returns:
        A styled bracketed badge, e.g. ``[stored]`` or
        ``[env set: OPENAI_API_KEY]``.
    """
    return Text(f"[{_badge_text(status)}]", style=_BADGE_STYLES[status.state])


def format_auth_indicator(status: ProviderAuthStatus) -> str:
    """Format the ``/model`` provider-header indicator.

    Args:
        status: Provider readiness status.

    Returns:
        Short indicator text, or an empty string for a configured provider.
    """
    return _SHORT_STATES[status.state]


def _badge_text(status: ProviderAuthStatus) -> str:
    """Return the text inside the badge brackets.

    The environment case names the variable: when a key comes from the shell
    rather than the credential store, `/auth` cannot replace it, so the user
    needs to know which variable to change instead.
    """
    if status.state is ProviderAuthState.CONFIGURED:
        if status.source is ProviderAuthSource.ENV and status.env_var:
            return f"env set: {status.env_var}"
        return "stored"
    if status.state is ProviderAuthState.MISSING:
        return "missing"
    return _SHORT_STATES[status.state]
