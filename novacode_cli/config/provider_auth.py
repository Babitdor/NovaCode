"""Provider credential readiness — the one place that answers "can we call this?".

Providers and services are configured in several ways at once: a key in the OS
keychain, an exported environment variable, no key at all (Ollama), or an
endpoint that carries its own auth. Rendering that as a boolean loses the
distinction the UI needs — ``/auth`` badges and the ``/model`` provider headers
must say *why* a provider is or is not usable, and "missing" and "no key
required" read very differently to a user staring at a key input.

:func:`get_provider_auth_status` resolves that into a
:class:`ProviderAuthStatus`, and it is the only supported way to ask.
Reading ``os.environ`` directly is what made a keychain-saved key look missing
after a restart: the export into ``os.environ`` happens at startup, so any
check that runs before or outside that pass sees nothing.

Services are not chat model providers — they back features
such as web search — but their credentials are stored the same way, so they
appear in ``/auth`` alongside the providers.

LangSmith is deliberately absent: Nova reads ``LANGSMITH_API_KEY`` from the
environment only, never from the keychain, so a key accepted here would look
saved and do nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from novacode_cli.config.credentials import CredentialStore


class ProviderAuthState(StrEnum):
    """Why a provider is or is not usable right now."""

    CONFIGURED = "configured"
    """Credentials were found — see :attr:`ProviderAuthStatus.source`."""

    MISSING = "missing"
    """A credential is required and none was found."""

    NOT_REQUIRED = "not_required"
    """The provider needs no credential (a local endpoint, e.g. Ollama)."""

    MANAGED = "managed"
    """The provider authenticates itself; Nova cannot see or supply a key."""

    UNKNOWN = "unknown"
    """Nova knows nothing about this provider's credentials."""


class ProviderAuthSource(StrEnum):
    """Where a ``CONFIGURED`` credential came from."""

    STORED = "stored"
    """Entered through ``/auth`` and held in the OS credential store."""

    ENV = "env"
    """Supplied as an environment variable."""

    NONE = "none"
    """No credential (only paired with a non-``CONFIGURED`` state)."""


@dataclass(frozen=True)
class ProviderAuthStatus:
    """Credential readiness for one provider or service.

    Attributes:
        name: Provider id (``"anthropic"``) or service id (``"tavily"``).
        state: Whether the provider is usable and why not.
        source: Where a configured credential came from.
        env_var: The environment variable the credential is read as, when one
            is known.
        detail: Human-readable explanation, phrased for a UI badge or
            indicator. ``None`` when the state needs no elaboration.
    """

    name: str
    state: ProviderAuthState
    source: ProviderAuthSource = ProviderAuthSource.NONE
    env_var: str | None = None
    detail: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether a model for this provider could be constructed now."""
        return self.state in (
            ProviderAuthState.CONFIGURED,
            ProviderAuthState.NOT_REQUIRED,
            ProviderAuthState.MANAGED,
        )

    def as_legacy_bool(self) -> bool | None:
        """Reduce the status to the historic tri-state contract.

        Returns:
            ``True`` when usable, ``False`` when a known credential is absent,
                ``None`` when readiness cannot be determined.
        """
        if self.state is ProviderAuthState.MISSING:
            return False
        if self.state is ProviderAuthState.UNKNOWN:
            return None
        return True


SERVICE_API_KEY_ENV: dict[str, str] = {
    "tavily": "TAVILY_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "jev": "TYPESAFE_API_KEY",
    "systemone": "SYSTEM_ONE_API_KEY",
}
"""Non-model services configurable through ``/auth``, mapped to their env var.

These back features rather than chat models — Tavily gates the ``web_search``
tool, Deepgram and ElevenLabs back the cloud voice backends — but their
credentials are stored exactly like a provider key, so they belong in the same
manager. Without them here, a voice key can only live in plaintext in
``Nova.config.json``.
"""

SERVICE_DISPLAY_NAMES: dict[str, str] = {
    "tavily": "Tavily (web search)",
    "deepgram": "Deepgram (speech to text)",
    "elevenlabs": "ElevenLabs (text to speech)",
    "jev": "Jev (System One compaction)",
    "systemone": "Custom System One (optional API key)",
}
"""Capitalized names for the services in ``/auth``.

Services have no provider preset to carry a display name, and showing the raw
config key ("tavily") next to "OpenAI"/"Anthropic" reads like a bug.
"""


def service_env_var(service: str) -> str | None:
    """Return the env var holding a service's credential.

    Args:
        service: Service id (e.g. ``"tavily"``).

    Returns:
        The environment variable name, or ``None`` when the service is unknown.
    """
    return SERVICE_API_KEY_ENV.get(service)


def is_service(name: str) -> bool:
    """Whether *name* is a service rather than a model provider.

    Args:
        name: A credential name from either registry.

    Returns:
        ``True`` for a service id.
    """
    return name in SERVICE_API_KEY_ENV


def credential_env_var(name: str) -> str | None:
    """Return the env var holding credentials for a provider or service.

    Providers come from :data:`novacode_cli.config.model_manager.MODEL_PRESETS`
    (imported lazily: ``model_manager`` imports this module's status function in
    turn). Providers that need no key — Ollama — report ``None``.

    Args:
        name: Provider id or service id.

    Returns:
        The environment variable name, or ``None`` when unknown or not
        applicable.
    """
    service_var = SERVICE_API_KEY_ENV.get(name)
    if service_var is not None:
        return service_var
    preset = _preset(name)
    if not preset:
        return None
    var = preset.get("api_key_var")
    return var if isinstance(var, str) and var else None


def provider_display_name(name: str) -> str:
    """Return the name shown for a provider or service in the UI.

    Args:
        name: Provider id or service id.

    Returns:
        The display name, falling back to the id itself when unknown.
    """
    preset = _preset(name)
    if preset:
        display = preset.get("name")
        if isinstance(display, str) and display:
            return display
    return SERVICE_DISPLAY_NAMES.get(name, name)


def credential_names() -> dict[str, str]:
    """Return every credential Nova knows about, as ``name -> env var``.

    Providers that require no key are omitted: there is nothing to store, so
    listing them in the credential manager would offer a key input for a
    provider that ignores it.

    Returns:
        Mapping of provider/service id to its environment variable.
    """
    names: dict[str, str] = {}
    for provider, preset in _presets().items():
        if not preset.get("requires_api_key"):
            continue
        var = preset.get("api_key_var")
        if isinstance(var, str) and var:
            names[provider] = var
    names.update(SERVICE_API_KEY_ENV)
    return names


def get_provider_auth_status(
    name: str,
    *,
    store: CredentialStore | None = None,
) -> ProviderAuthStatus:
    """Return credential readiness for a provider or service.

    Resolution order: unknown name -> ``UNKNOWN``; no key required ->
    ``NOT_REQUIRED``; a key in the credential store -> ``CONFIGURED`` from
    ``STORED``; a key in the environment -> ``CONFIGURED`` from ``ENV``; else
    ``MISSING``.

    The store is checked before the environment so the badge can tell the user
    where the key came from — the two are both "configured", but only one of
    them can be replaced from inside ``/auth``.

    Args:
        name: Provider id (``"anthropic"``) or service id (``"tavily"``).
        store: Credential store to reuse across a pass over many names.
            ``None`` builds a single-use one.

    Returns:
        The provider's readiness status.
    """
    preset = _preset(name)
    if preset is None and not is_service(name):
        return ProviderAuthStatus(
            name=name,
            state=ProviderAuthState.UNKNOWN,
            detail="credentials unknown",
        )

    env_var = credential_env_var(name)
    if preset is not None and not preset.get("requires_api_key"):
        return ProviderAuthStatus(
            name=name,
            state=ProviderAuthState.NOT_REQUIRED,
            detail="no API key required",
        )

    if env_var is None:
        return ProviderAuthStatus(
            name=name,
            state=ProviderAuthState.UNKNOWN,
            detail="credentials unknown",
        )

    store = store if store is not None else CredentialStore()
    if store.has_key(env_var):
        return ProviderAuthStatus(
            name=name,
            state=ProviderAuthState.CONFIGURED,
            source=ProviderAuthSource.STORED,
            env_var=env_var,
            detail="stored",
        )

    if _env_value(env_var):
        return ProviderAuthStatus(
            name=name,
            state=ProviderAuthState.CONFIGURED,
            source=ProviderAuthSource.ENV,
            env_var=env_var,
            detail=f"set in environment: {env_var}",
        )

    return ProviderAuthStatus(
        name=name,
        state=ProviderAuthState.MISSING,
        env_var=env_var,
        detail=f"{env_var} is not set or is empty",
    )


def get_all_auth_statuses(
    *,
    include_providers_without_keys: bool = False,
) -> dict[str, ProviderAuthStatus]:
    """Return readiness for every known provider and service.

    One :class:`CredentialStore` is built and reused for the whole pass, so the
    keychain backend is probed once rather than once per name.

    Args:
        include_providers_without_keys: Also report providers that need no
            credential (Ollama), which the credential manager hides.

    Returns:
        Statuses keyed by provider/service id, in registry order.
    """
    store = CredentialStore()
    names: list[str] = list(_presets())
    if not include_providers_without_keys:
        names = [name for name in names if name in credential_names()]
    names.extend(name for name in SERVICE_API_KEY_ENV if name not in names)
    return {name: get_provider_auth_status(name, store=store) for name in names}


def _presets() -> dict[str, dict[str, object]]:
    """Return the provider preset registry, imported lazily to avoid a cycle."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    return MODEL_PRESETS


def _preset(name: str) -> dict[str, object] | None:
    """Return one provider preset, or ``None`` when *name* is not a provider."""
    return _presets().get(name)


def _env_value(env_var: str) -> str | None:
    """Read an environment variable, treating empty as unset."""
    return os.environ.get(env_var) or None
