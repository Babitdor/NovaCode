"""Check a provider's settings before first-run setup saves them.

Setup used to save whatever was typed: a wrong API key, or an Ollama host with
nothing listening, both ended in "setup complete", and the user found out on
their first prompt, as a provider traceback. One cheap request here turns that
into a sentence on the setup screen.

Every check is a single GET with a short timeout. None of them sends a prompt or
costs anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

TIMEOUT_S = 6.0
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

_REJECTED = (401, 403)


@dataclass
class Check:
    """What a check found.

    ``ok`` is True when the settings work, False when they are known to be wrong
    (key rejected, nothing listening), and None when it could not be decided
    (no network). Only True lets setup finish without asking.
    """

    ok: bool | None
    message: str
    models: list[str] = field(default_factory=list)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _key_request(provider: str, key: str) -> tuple[str, dict[str, str], tuple[int, ...]] | None:
    """``(url, headers, statuses that mean "rejected")`` for *provider*, or None.

    None means the provider has no endpoint that tells a good key from a bad one
    without spending tokens (NVIDIA's model list is public).
    """
    from novacode_cli.config.model_manager import OPENCODE_BASE_URL, OPENROUTER_BASE_URL

    if provider == "openai":
        return "https://api.openai.com/v1/models", _bearer(key), _REJECTED
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        return "https://api.anthropic.com/v1/models", headers, _REJECTED
    if provider == "google":
        # The key travels in a header, never the URL: URLs end up in logs.
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        return url, {"x-goog-api-key": key}, (400, 401, 403)
    if provider == "openrouter":
        return f"{OPENROUTER_BASE_URL}/key", _bearer(key), _REJECTED
    if provider == "opencode":
        return f"{OPENCODE_BASE_URL}/models", _bearer(key), _REJECTED
    return None


def check_ollama(host: str) -> Check:
    """Is an Ollama server answering at *host*, and does it have a model?"""
    host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
    try:
        response = requests.get(f"{host}/api/tags", timeout=TIMEOUT_S)
    except requests.RequestException:
        return Check(
            False,
            f"Nothing is answering at {host}. Start Ollama (`ollama serve`), "
            "fix the host, or choose a cloud provider.",
        )
    if response.status_code != requests.codes.ok:
        return Check(False, f"{host} answered HTTP {response.status_code}, which is not an Ollama server.")
    try:
        models = [str(m["name"]) for m in response.json().get("models", []) if m.get("name")]
    except (ValueError, TypeError, KeyError, AttributeError):
        models = []
    if not models:
        return Check(
            False,
            "Ollama is running but has no models. Pull one (`ollama pull <model>`), then press Finish again.",
        )
    return Check(True, f"Ollama is running with {len(models)} model(s).", models)


def check_key(provider: str, key: str) -> Check:
    """Does *provider* accept *key*?"""
    request = _key_request(provider, key)
    if request is None:
        return Check(True, "Saved. This provider's key cannot be checked without sending a prompt.")
    url, headers, rejected = request
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT_S)
    except requests.RequestException:
        return Check(None, "Could not reach the provider to check the key. Are you online?")
    if response.status_code in rejected:
        return Check(False, f"{provider} rejected that API key (HTTP {response.status_code}). Check it and try again.")
    if response.status_code == requests.codes.ok:
        return Check(True, "API key accepted.")
    return Check(None, f"The provider answered HTTP {response.status_code}, so the key could not be checked.")


def check_provider(provider: str, value: str) -> Check:
    """Check the one thing setup collects: an Ollama host, or a provider's API key."""
    return check_ollama(value) if provider == "ollama" else check_key(provider, value)


def pick_ollama_model(models: list[str]) -> str:
    """The model to start on: Nova's default when it is installed, else the first one."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    default = str((MODEL_PRESETS.get("ollama") or {}).get("default_model") or "")
    if default in models or not models:
        return default
    return models[0]
