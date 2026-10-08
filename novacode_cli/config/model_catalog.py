"""The model lineup offered by ``/model``, per provider.

Two kinds of source feed this, and both exist because the other is wrong for
some provider:

- **Provider profile data.** The LangChain provider packages ship an
  auto-generated ``data/_profiles.py`` (derived from models.dev) listing every
  model they support with capability flags. It is offline, current with the
  installed package, and costs no network call. It is the source for the
  providers Nova constructs directly (OpenAI, Anthropic, Google, NVIDIA).
- **A live list.** Ollama knows which models are actually installed, and
  OpenCode Go knows which of its gateway's ids are live — a hand-maintained
  preset list for either drifts into ids that 400 on first use. Those two ask
  the provider itself.

The curated list in
:data:`novacode_cli.config.model_manager.MODEL_PRESETS` is unioned into every
result and ordered first. It is not a fallback: it holds the ids Nova's own
defaults and vision routing name, so dropping it because a profile list is
longer would silently remove the models Nova is configured to use.

Profile modules are loaded by file path rather than imported. Importing
``langchain_openai.data._profiles`` runs ``langchain_openai/__init__.py`` first,
which pulls the provider SDK — tens of megabytes and seconds of startup on the
TUI path, for a dictionary of strings.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_PROFILE_MODULES: dict[str, str] = {
    "openai": "langchain_openai.data._profiles",
    "anthropic": "langchain_anthropic.data._profiles",
    "google": "langchain_google_genai.data._profiles",
    "nvidia": "langchain_nvidia_ai_endpoints.data._profiles",
}
"""Providers whose model list comes from their package's profile data.

Spelled out rather than derived from LangChain's provider registry: Nova's
provider ids are its own (``google``, not ``google_genai``), Nova never calls
``init_chat_model``, and deriving the mapping would silently pick up providers
Nova cannot construct a client for.
"""

PROVIDERS_WITH_LIVE_LISTS: frozenset[str] = frozenset({"ollama", "opencode", "opencode_zen"})
"""Providers asked directly for their model list instead of read from profiles."""

_CACHE_TTL_SECONDS = 300.0
"""How long a resolved model list stays fresh.

A live list shells out (``ollama list``) or makes an HTTP call, and reopening
``/model`` is a common action — re-running either on every open would make the
picker feel slow and hammer the gateway. Long enough to cover a session's worth
of opens, short enough that a model installed meanwhile appears without a
restart.
"""

_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()
_profiles_cache: dict[str, dict[str, Any]] = {}


def get_provider_models(provider: str, *, refresh: bool = False) -> list[str]:
    """Return the model ids to offer for *provider*.

    Args:
        provider: Provider id (``"anthropic"``, ``"ollama"``, ...).
        refresh: Ignore the cache and re-resolve the list.

    Returns:
        Model ids, curated entries first. Never raises: a provider whose list
        cannot be resolved falls back to its curated preset list, so the picker
        can always render something selectable.
    """
    from novacode_cli.config.model_manager import MODEL_PRESETS

    preset = MODEL_PRESETS.get(provider)
    curated = _curated_models(provider)
    if preset is None:
        return curated

    cached = _cache_get(provider, refresh=refresh)
    if cached is not None:
        return cached

    discovered: list[str] = []
    try:
        if provider in PROVIDERS_WITH_LIVE_LISTS:
            discovered = _live_models(provider)
        else:
            discovered = _profile_models(provider)
    except Exception:  # noqa: BLE001 — one bad provider must not empty the picker
        logger.warning("Could not resolve models for provider '%s'", provider, exc_info=True)
        discovered = []

    models = _merge(curated, discovered)
    _cache_put(provider, models)
    return models


def get_all_models(*, refresh: bool = False) -> dict[str, list[str]]:
    """Return every provider's model list, in registry order.

    Args:
        refresh: Ignore the cache and re-resolve every list.

    Returns:
        Model ids keyed by provider id.
    """
    from novacode_cli.config.model_manager import MODEL_PRESETS

    return {provider: get_provider_models(provider, refresh=refresh) for provider in MODEL_PRESETS}


def clear_catalog_cache() -> None:
    """Drop cached model lists and loaded profile data."""
    with _cache_lock:
        _cache.clear()
        _profiles_cache.clear()


def _curated_models(provider: str) -> list[str]:
    """Return the hand-maintained model ids from the provider preset."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    preset = MODEL_PRESETS.get(provider) or {}
    models = preset.get("models") or []
    return [str(name) for name in models]


def _live_models(provider: str) -> list[str]:
    """Ask the provider for its own list (Ollama, OpenCode Go)."""
    from novacode_cli.config.model_manager import get_ollama_models, get_opencode_models

    if provider == "ollama":
        return list(get_ollama_models())
    if provider == "opencode_zen":
        return list(get_opencode_models(provider))
    return list(get_opencode_models())


def _profile_models(provider: str) -> list[str]:
    """Return tool-capable chat model ids from the provider's profile data."""
    module_path = PROVIDER_PROFILE_MODULES.get(provider)
    if not module_path:
        return []
    profiles = _load_profiles(module_path)
    return sorted(
        name
        for name, profile in profiles.items()
        if profile.get("tool_calling", False)
        and profile.get("text_inputs", True) is not False
        and profile.get("text_outputs", True) is not False
    )


def _load_profiles(module_path: str) -> dict[str, Any]:
    """Load ``_PROFILES`` from a provider's data module without importing it.

    Importing the module normally would import its package first, dragging the
    provider SDK onto the path. Only the one file is read, located from the
    package spec.

    Args:
        module_path: Dotted path (e.g. ``"langchain_openai.data._profiles"``).

    Returns:
        The ``_PROFILES`` mapping, or an empty dict when the package is not
        installed or the attribute is absent.
    """
    with _cache_lock:
        cached = _profiles_cache.get(module_path)
    if cached is not None:
        return cached

    parts = module_path.split(".")
    spec = importlib.util.find_spec(parts[0])
    if spec is None:
        logger.debug("Provider package '%s' is not installed", parts[0])
        return {}

    if spec.origin:
        package_dir = Path(spec.origin).parent
    elif spec.submodule_search_locations:
        package_dir = Path(next(iter(spec.submodule_search_locations)))
    else:
        logger.debug("Cannot locate package '%s' on disk", parts[0])
        return {}

    profiles_path = package_dir.joinpath(*parts[1:-1], f"{parts[-1]}.py")
    if not profiles_path.exists():
        logger.debug("No profile data at %s", profiles_path)
        return {}

    file_spec = importlib.util.spec_from_file_location(module_path, profiles_path)
    if file_spec is None or file_spec.loader is None:
        logger.debug("Cannot load profile data from %s", profiles_path)
        return {}

    module = importlib.util.module_from_spec(file_spec)
    file_spec.loader.exec_module(module)
    profiles: dict[str, Any] = getattr(module, "_PROFILES", {}) or {}
    with _cache_lock:
        _profiles_cache[module_path] = profiles
    return profiles


def _merge(curated: list[str], discovered: list[str]) -> list[str]:
    """Combine curated and discovered ids, curated first and de-duplicated.

    Args:
        curated: Hand-maintained ids; these keep their configured order.
        discovered: Ids from profiles or a live list; sorted by the caller.

    Returns:
        The merged list, curated first.
    """
    merged = list(dict.fromkeys(curated))
    seen = set(merged)
    for name in discovered:
        if name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def _cache_get(provider: str, *, refresh: bool) -> list[str] | None:
    """Return a still-fresh cached list, or ``None``."""
    if refresh:
        return None
    with _cache_lock:
        entry = _cache.get(provider)
    if entry is None:
        return None
    stamped, models = entry
    if time.monotonic() - stamped > _CACHE_TTL_SECONDS:
        return None
    return list(models)


def _cache_put(provider: str, models: list[str]) -> None:
    """Record a resolved list with its timestamp."""
    with _cache_lock:
        _cache[provider] = (time.monotonic(), list(models))
