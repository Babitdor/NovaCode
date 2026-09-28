"""Credential storage for model providers and non-model services.

Credentials entered in the TUI have to survive a restart, be readable by more
than one process, and be deletable. Nova already has that storage —
:class:`novacode_cli.onboarding.SecretManager` (the OS keychain, with a
permission-restricted JSON fallback) — so this module does not introduce a
second secret backend. It adds the two things the keychain alone cannot express:

1. **An endpoint and a project paired with the key.** ``SecretManager`` stores
   one opaque string per name, so the endpoint a personal key belongs to has
   nowhere to live. It is kept beside the key in ``Nova.config.json`` as
   non-secret metadata, never in the keychain, and never mixed into the secret
   value (every existing reader copies that value straight into ``os.environ``,
   so a JSON-wrapped value would poison the environment).

2. **Enumeration and deletion.** ``keyring`` cannot list entries, so a caller
   must say which names to probe; :meth:`CredentialStore.list_meta` does exactly
   that over a caller-supplied name set. A key stored under a name the
   registry does not know about is therefore invisible here — the same
   limitation ``SecretManager.list_secrets`` already documents.

Secrets are never logged, formatted with ``!r``, or interpolated into an
exception message; every helper reports structural facts only
("set credential for ANTHROPIC_API_KEY"). Failures are returned in
:class:`WriteOutcome` rather than raised, because ``logger.warning`` is
invisible inside a Textual session — the UI has to render them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from novacode_cli.onboarding import SecretManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CredentialMeta:
    """The non-secret facts recorded alongside a stored credential.

    Keyed by environment variable name, not by provider, because that is the
    stable identifier the secret itself is filed under.

    Attributes:
        env_var: The environment variable the credential is read as.
        base_url: Endpoint paired with the key, or ``None`` for the provider
            default. Only meaningful for providers that accept a custom
            endpoint.
        project: Optional project/workspace name paired with the key.
        added_at: ISO-8601 UTC timestamp of the last write.
    """

    env_var: str
    base_url: str | None = None
    project: str | None = None
    added_at: str | None = None


@dataclass
class WriteOutcome:
    """Result of a credential write.

    Attributes:
        ok: Whether the secret itself reached the store. Metadata failures are
            still reported as warnings on a successful write, because a lost
            endpoint is a broken pairing, not a lost credential.
        warnings: Problems the user must see — chiefly a keychain write that
            reported failure. Empty on a clean write.
    """

    ok: bool
    warnings: list[str] = field(default_factory=list)


def env_var_to_secret_name(env_var: str) -> str:
    """Return the keyring entry name for an environment variable.

    The keychain is keyed by the lowercased env var name, which is what
    ``API_KEY_NAMES`` and :func:`novacode_cli.onboarding.load_secrets_into_env`
    already use (``OPENAI_API_KEY`` -> ``openai_api_key``).

    Args:
        env_var: The environment variable name (e.g. ``OPENAI_API_KEY``).

    Returns:
        The secret name to file the value under.
    """
    return env_var.lower()


class CredentialStore:
    """Credential reads and writes bound to one secret manager.

    Binding matters for cost, not just style: every status or availability pass
    covers the whole provider registry, and a fresh ``SecretManager`` per probe
    re-runs keyring backend detection each time. Callers doing a pass construct
    one store and reuse it. ``None`` stands for "no keychain backend available"
    — reads then report nothing stored and writes report a warning, rather than
    raising into a TUI handler.
    """

    def __init__(self, manager: SecretManager | None = None) -> None:
        """Initialize the store.

        Args:
            manager: An existing secret manager to reuse, or ``None`` to
                obtain one lazily on first use.
        """
        self._manager = manager
        self._resolved = manager is not None

    @property
    def manager(self) -> SecretManager | None:
        """The secret manager, resolving one on first access when needed."""
        if not self._resolved:
            self._manager = _default_manager()
            self._resolved = True
        return self._manager

    def get_key(self, env_var: str) -> str | None:
        """Read a stored secret without touching the process environment.

        Deliberately does *not* export the value: this is the read used by
        status and availability checks, which must have no side effects, and by
        callers that only want to know whether a credential exists.

        Args:
            env_var: The environment variable the credential is filed under.

        Returns:
            The stored key, or ``None`` when nothing is stored.
        """
        if self.manager is None:
            return None
        try:
            value = self.manager.get_secret(env_var_to_secret_name(env_var))
        except Exception:  # noqa: BLE001 — a broken keyring must not break the caller
            logger.warning("Could not read stored credential for %s", env_var)
            return None
        return value or None

    def has_key(self, env_var: str) -> bool:
        """Whether a credential is stored for *env_var*.

        Args:
            env_var: The environment variable the credential is filed under.

        Returns:
            ``True`` when a non-empty secret is stored.
        """
        return self.get_key(env_var) is not None

    def set_key(
        self,
        env_var: str,
        key: str,
        *,
        base_url: str | None = None,
        project: str | None = None,
    ) -> WriteOutcome:
        """Store a credential and pair its endpoint/project with it.

        Args:
            env_var: The environment variable the credential is filed under.
            key: The secret value as entered by the user.
            base_url: Endpoint to pair with the key, or ``None`` for the
                provider default. Passing ``None`` clears a previously stored
                endpoint, so the key is never left pointing at an endpoint the
                user just removed.
            project: Optional project/workspace name to pair with the key.

        Returns:
            A :class:`WriteOutcome`; ``ok`` is ``False`` only when the secret
            itself could not be stored.
        """
        if not key.strip():
            return WriteOutcome(ok=False, warnings=["API key cannot be empty."])

        outcome = WriteOutcome(ok=True)
        if self.manager is None:
            outcome.ok = False
            outcome.warnings.append(
                "No credential store is available on this system; the key was not saved."
            )
            return outcome

        try:
            stored = self.manager.store_secret(env_var_to_secret_name(env_var), key)
        except Exception:  # noqa: BLE001 — report, never raise into a UI handler
            logger.warning("Could not write credential for %s", env_var)
            stored = False
        if not stored:
            outcome.ok = False
            outcome.warnings.append(
                f"Could not save the credential for {env_var}; "
                "the OS credential store rejected the write."
            )
            return outcome

        self._write_meta(env_var, base_url=base_url, project=project, warnings=outcome.warnings)
        # Applied here rather than left to the caller: model construction reads
        # `os.environ`, and a credential that reached the store but not the
        # environment would look saved and still fail until a restart. The
        # metadata is written first because the export reads the paired endpoint.
        try:
            self.apply_to_env(env_var)
        except Exception:  # noqa: BLE001 — the key is stored; the export is best-effort
            logger.warning("Could not apply credential for %s to the environment", env_var)
            outcome.warnings.append(
                f"Saved {env_var}, but it could not be applied to this session. "
                "Restart Nova to use it."
            )
        return outcome

    def delete(self, env_var: str) -> bool:
        """Delete a stored credential, its metadata, and its env vars.

        The environment entries are removed too: the running process would
        otherwise keep using a key the user just deleted, and a leftover
        endpoint would keep pointing the provider at it.

        Args:
            env_var: The environment variable the credential is filed under.

        Returns:
            ``True`` when the key is gone (including when it was never stored).
        """
        removed = True
        if self.manager is not None:
            try:
                removed = bool(self.manager.delete_secret(env_var_to_secret_name(env_var)))
            except Exception:  # noqa: BLE001 — report, never raise into a UI handler
                logger.warning("Could not delete credential for %s", env_var)
                removed = False
        self._delete_meta(env_var)
        os.environ.pop(env_var, None)
        endpoint_var = _base_url_env_var(env_var)
        if endpoint_var is not None:
            os.environ.pop(endpoint_var, None)
        return removed

    def meta(self, env_var: str) -> CredentialMeta | None:
        """Return stored metadata for *env_var*, if any.

        Args:
            env_var: The environment variable the credential is filed under.

        Returns:
            The metadata, or ``None`` when nothing was recorded.
        """
        return self.list_meta([env_var]).get(env_var)

    def list_meta(self, env_vars: Iterable[str]) -> dict[str, CredentialMeta]:
        """Return metadata for every named credential that has some.

        Only credentials that recorded something (an endpoint, a project) are
        returned: a plain key writes no metadata, so it never reaches disk.

        Args:
            env_vars: Candidate environment variable names to look up.

        Returns:
            Metadata keyed by environment variable name.
        """
        from novacode_cli.config.nova_config import NovaConfig

        try:
            recorded = NovaConfig().get_credential_meta()
        except Exception:  # noqa: BLE001 — a corrupt config must not break a status pass
            logger.warning("Could not read credential metadata")
            return {}

        result: dict[str, CredentialMeta] = {}
        for env_var in env_vars:
            raw = recorded.get(env_var)
            if not isinstance(raw, dict):
                continue
            result[env_var] = CredentialMeta(
                env_var=env_var,
                base_url=_clean(raw.get("base_url")) or None,
                project=_clean(raw.get("project")) or None,
                added_at=_clean(raw.get("added_at")) or None,
            )
        return result

    def apply_to_env(self, env_var: str) -> str | None:
        """Export a stored credential into the process environment.

        Model construction reads keys from ``os.environ``, and the base URL env
        var is set from the paired endpoint when one was stored — so a rotation
        that removed the endpoint also clears the exported URL instead of
        leaving the old one in effect.

        Args:
            env_var: The environment variable the credential is filed under.

        Returns:
            The exported key, or ``None`` when nothing is stored.
        """
        key = self.get_key(env_var)
        if not key:
            return None
        os.environ[env_var] = key
        meta = self.meta(env_var)
        endpoint_var = _base_url_env_var(env_var)
        if endpoint_var is not None:
            if meta is not None and meta.base_url:
                os.environ[endpoint_var] = meta.base_url
            else:
                os.environ.pop(endpoint_var, None)
        return key

    def _write_meta(
        self,
        env_var: str,
        *,
        base_url: str | None,
        project: str | None,
        warnings: list[str],
    ) -> None:
        """Persist metadata, or drop the record when there is none to keep."""
        from novacode_cli.config.nova_config import NovaConfig

        try:
            config = NovaConfig()
            if base_url or project:
                config.set_credential_meta(
                    env_var,
                    base_url=base_url,
                    project=project,
                    added_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            else:
                config.delete_credential_meta(env_var)
        except Exception:  # noqa: BLE001 — a lost endpoint is not a lost credential
            logger.warning("Could not record credential metadata for %s", env_var)
            warnings.append(
                f"Saved the credential for {env_var}, but its endpoint could not be recorded."
            )

    @staticmethod
    def _delete_meta(env_var: str) -> None:
        """Drop metadata for *env_var*, ignoring an unreadable config."""
        from novacode_cli.config.nova_config import NovaConfig

        try:
            NovaConfig().delete_credential_meta(env_var)
        except Exception:  # noqa: BLE001 — deletion of the key already succeeded
            logger.warning("Could not clear credential metadata for %s", env_var)


#: Endpoint env var per credential env var, for providers that accept one.
#:
#: Only OpenAI honours a custom endpoint in Nova (see
#: :data:`novacode_cli.config.model_manager.ENDPOINT_PROVIDERS`); the gateways
#: pin their own base URL and Anthropic/Google/Ollama take none at all. Kept as
#: a mapping rather than a literal so a second endpoint-capable provider is a
#: one-line addition here and in the provider preset.
_BASE_URL_ENV_VARS: dict[str, str] = {
    "OPENAI_API_KEY": "OPENAI_BASE_URL",
}


def _base_url_env_var(env_var: str) -> str | None:
    """Return the endpoint env var paired with a credential env var."""
    return _BASE_URL_ENV_VARS.get(env_var)


def _clean(value: object) -> str:
    """Normalize a recorded metadata field to a stripped string."""
    return value.strip() if isinstance(value, str) else ""


def _default_manager() -> SecretManager | None:
    """Build a secret manager, or ``None`` when no backend is usable.

    Imported here rather than at module scope for two reasons: ``onboarding``
    imports back into ``config``, and tests replace
    ``novacode_cli.onboarding.SecretManager`` — a module-scope binding would
    capture the real class before that patch lands.
    """
    try:
        from novacode_cli.onboarding import SecretManager
    except Exception:  # noqa: BLE001 — a missing keyring is not a hard failure
        logger.debug("Secret storage is unavailable; credentials cannot be read")
        return None
    try:
        return SecretManager()
    except Exception:  # noqa: BLE001 — a broken keyring backend must degrade, not crash
        logger.warning("OS credential store is unavailable; using no credential store")
        return None


def default_store() -> CredentialStore:
    """Return a credential store over the default secret manager."""
    return CredentialStore()


# ---------------------------------------------------------------------------
# Convenience wrappers — one secret manager per call
#
# For a single lookup. A pass over the whole registry should build one
# `CredentialStore` and reuse it (see `provider_auth`).
# ---------------------------------------------------------------------------


def get_stored_key(env_var: str) -> str | None:
    """Read a stored credential without exporting it (see `get_key`)."""
    return default_store().get_key(env_var)


def has_stored_credential(env_var: str) -> bool:
    """Whether a credential is stored for *env_var*."""
    return default_store().has_key(env_var)


def set_credential(
    env_var: str,
    key: str,
    *,
    base_url: str | None = None,
    project: str | None = None,
) -> WriteOutcome:
    """Store a credential and pair its endpoint with it."""
    return default_store().set_key(env_var, key, base_url=base_url, project=project)


def delete_credential(env_var: str) -> bool:
    """Delete a stored credential, its metadata, and its env var."""
    return default_store().delete(env_var)


def credential_meta(env_var: str) -> CredentialMeta | None:
    """Return stored metadata for *env_var*, if any."""
    return default_store().meta(env_var)


def apply_credential_to_env(env_var: str) -> str | None:
    """Export a stored credential into the process environment."""
    return default_store().apply_to_env(env_var)


def credential_value(env_var: str) -> str:
    """The stored credential for *env_var*, else the environment, else ``""``.

    The order is the point. A key entered through the UI lives in the keychain,
    and only the startup hydration pass puts it in ``os.environ``, so a caller
    reading the environment alone sees nothing until the process restarts. This
    is the lookup for code that was written against ``os.environ`` directly.

    Args:
        env_var: The environment variable the credential is filed under.

    Returns:
        The key, or an empty string when none is available anywhere.
    """
    stored = get_stored_key(env_var)
    if stored:
        return stored
    return os.environ.get(env_var) or ""


#: Cloud voice providers, mapped to their credential env var and the config
#: provider id those keys used to be written to. Both lived in plaintext in
#: ``Nova.config.json`` under ``voice.providers.<id>``.
_LEGACY_VOICE_KEYS: dict[str, str] = {
    "DEEPGRAM_API_KEY": "deepgram",
    "ELEVENLABS_API_KEY": "elevenlabs",
}


def migrate_voice_keys() -> list[str]:
    """Move plaintext voice keys from the config file into the keychain.

    Earlier releases stored these under ``voice.providers.<id>.api_key`` (and a
    legacy ``.key`` duplicate), because the voice backends read their key from
    the config rather than the environment. The value is copied to the keychain
    and the config field is blanked, so the file stops carrying a secret.

    Idempotent, and safe to call on every startup: a provider with no plaintext
    key is skipped, and one whose key is already stored only has the redundant
    copy cleared. Writes to the config are non-fatal — a failure leaves the
    plaintext in place, which the pipeline still honors via its fallback chain.

    Returns:
        The env var names whose plaintext copy was removed, for reporting.
    """
    from novacode_cli.config.nova_config import NovaConfig

    migrated: list[str] = []
    try:
        config = NovaConfig()
        store = default_store()
    except Exception:  # noqa: BLE001 — startup must not fail on a config problem
        logger.warning("Could not open the config for voice key migration")
        return migrated

    for env_var, provider in _LEGACY_VOICE_KEYS.items():
        try:
            provider_cfg = config.get_voice_provider_config(provider)
        except Exception:  # noqa: BLE001
            logger.warning("Could not read voice config for %s", provider)
            continue
        plaintext = _clean(provider_cfg.get("api_key")) or _clean(provider_cfg.get("key"))
        if not plaintext:
            continue
        try:
            if not store.has_key(env_var):
                outcome = store.set_key(env_var, plaintext)
                if not outcome.ok:
                    logger.warning("Could not store the %s key", env_var)
                    continue
            # Blank both fields: `api_key` and the legacy `key` alias the older
            # `/voice settings --key` wrote.
            config.set_voice_provider_config(provider, api_key="", key="")
        except Exception:  # noqa: BLE001
            logger.warning("Could not move the %s key out of the config", env_var)
            continue
        migrated.append(env_var)
    return migrated
