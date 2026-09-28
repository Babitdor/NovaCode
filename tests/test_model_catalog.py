"""The model lineup behind ``/model``: profiles for the rest, live for two.

Provider profile data ships inside the LangChain packages and is offline, so it
is the default source. Ollama and OpenCode Go are the exceptions — only the
provider knows what is installed or currently served — and the curated preset
list is unioned into every result so an id Nova's own defaults name can never
disappear behind a longer discovered list.
"""

from __future__ import annotations

import sys

import pytest

from novacode_cli.config import model_catalog
from novacode_cli.config.model_manager import MODEL_PRESETS


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts from an empty cache; the real TTL is minutes long."""
    model_catalog.clear_catalog_cache()
    yield
    model_catalog.clear_catalog_cache()


def test_curated_ids_come_first():
    """Nova's own defaults and vision routing name these ids."""
    for provider in ("openai", "anthropic", "google", "openrouter"):
        curated = [str(name) for name in MODEL_PRESETS[provider]["models"]]
        models = model_catalog.get_provider_models(provider)

        assert models[: len(curated)] == curated, provider


def test_profile_backed_providers_gain_models_beyond_the_curated_list():
    for provider in ("openai", "anthropic", "google", "nvidia"):
        models = model_catalog.get_provider_models(provider)
        curated = [str(name) for name in MODEL_PRESETS[provider]["models"]]

        assert len(models) > len(curated), provider
        assert len(models) == len(set(models)), provider


def test_only_tool_capable_text_models_are_offered():
    """A model that cannot call tools cannot run the agent."""
    profiles = model_catalog._load_profiles("langchain_openai.data._profiles")
    curated = {str(name) for name in MODEL_PRESETS["openai"]["models"]}

    discovered = set(model_catalog.get_provider_models("openai")) - curated

    ineligible = {
        name
        for name, profile in profiles.items()
        if not profile.get("tool_calling", False)
        or profile.get("text_inputs", True) is False
        or profile.get("text_outputs", True) is False
    }
    # Both directions: nothing ineligible got in, and the filter was actually
    # doing work (an empty `ineligible` set would make the first assertion
    # vacuous).
    assert ineligible, "expected the profile data to contain non-tool models"
    assert not (discovered & ineligible)


def test_live_providers_are_asked_directly(monkeypatch):
    """The hand-maintained presets drift; the provider is the source of truth."""
    monkeypatch.setattr(
        "novacode_cli.config.model_manager.get_ollama_models",
        lambda: ["llama3.3:70b", "brand-new:latest"],
    )
    monkeypatch.setattr(
        "novacode_cli.config.model_manager.get_opencode_models",
        lambda: ["gateway-only-id"],
    )

    assert "brand-new:latest" in model_catalog.get_provider_models("ollama")
    assert "gateway-only-id" in model_catalog.get_provider_models("opencode")


def test_a_failing_provider_still_offers_its_curated_list(monkeypatch):
    def _boom() -> list[str]:
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(model_catalog, "_live_models", _boom)

    models = model_catalog.get_provider_models("opencode")

    assert models == [str(name) for name in MODEL_PRESETS["opencode"]["models"]]


def test_an_unresolvable_provider_never_raises(monkeypatch):
    monkeypatch.setattr(model_catalog, "_profile_models", lambda provider: 1 / 0)

    assert model_catalog.get_provider_models("anthropic")


def test_the_list_is_cached_between_calls(monkeypatch):
    calls: list[int] = []

    def _counted() -> list[str]:
        calls.append(1)
        return ["a", "b"]

    monkeypatch.setattr(model_catalog, "_live_models", lambda provider: _counted())

    model_catalog.get_provider_models("ollama")
    model_catalog.get_provider_models("ollama")

    assert len(calls) == 1


def test_refresh_bypasses_the_cache(monkeypatch):
    calls: list[int] = []

    def _counted() -> list[str]:
        calls.append(1)
        return ["a", "b"]

    monkeypatch.setattr(model_catalog, "_live_models", lambda provider: _counted())

    model_catalog.get_provider_models("ollama")
    model_catalog.get_provider_models("ollama", refresh=True)

    assert len(calls) == 2
    model_catalog.clear_catalog_cache()
    model_catalog.get_provider_models("ollama")
    assert len(calls) == 3


def test_profiles_are_read_without_importing_the_provider_package(monkeypatch):
    """Importing the module normally would drag the provider SDK in first.

    `langchain_openai/__init__.py` pulls the `openai` client: seconds of startup
    on the TUI path, for a dictionary of model names.

    The entry is removed first so this holds whichever test ran before — some
    other test in the suite does import the module properly, and asserting on a
    key that is already there would pass or fail by collection order.
    """
    monkeypatch.delitem(sys.modules, "langchain_openai.data._profiles", raising=False)

    profiles = model_catalog._load_profiles("langchain_openai.data._profiles")

    assert profiles
    assert "langchain_openai.data._profiles" not in sys.modules


def test_an_unknown_provider_has_no_models():
    assert model_catalog.get_provider_models("nonsense") == []


def test_every_registry_provider_resolves_to_something():
    """An empty list would render a provider header with no models under it."""
    models = model_catalog.get_all_models()

    assert set(models) == set(MODEL_PRESETS)
    assert all(ids for ids in models.values())
