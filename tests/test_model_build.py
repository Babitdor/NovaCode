"""build_chat_model is THE model constructor — pin its kwargs per provider.

Guards the consolidation of 20 drifted ChatX(...) construction sites into one
module: the drift-prone kwargs (retries, effort, num_ctx, base URL) are pinned
here, and the ModelManager path is pinned to equivalence with the direct path.
"""

import pytest

from novacode_cli.config.model_create import (
    PROVIDER_KEY_ENV,
    build_async_agent_model,
    build_chat_model,
    create_model_from_config,
)


class _StubNovaConfig:
    """NovaConfig stub: no reasoning effort, no thinking budget."""

    def __init__(self, *a, **k):
        pass

    def get(self, key, default=None):
        return None if key == "reasoning_effort" else (default if default is not None else 0)

    def get_model_config(self):
        return None


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch):
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _StubNovaConfig)
    monkeypatch.setattr("novacode_cli.context._dynamic.get_ollama_num_ctx", lambda: 8192)
    for var in PROVIDER_KEY_ENV.values():
        monkeypatch.setenv(var, "test-key")
    monkeypatch.setenv("Nova_THINKING_BUDGET", "0")


def test_openai_kwargs_pinned():
    m = build_chat_model("openai", "gpt-5-mini")
    assert type(m).__name__ == "ChatOpenAI"
    assert m.max_retries == 5


def test_openrouter_kwargs_pinned():
    m = build_chat_model("openrouter", "z-ai/glm-5.2")
    assert type(m).__name__ == "ChatOpenAI"
    assert m.max_retries == 5
    assert "openrouter" in (m.openai_api_base or "")


def test_anthropic_kwargs_pinned():
    m = build_chat_model("anthropic", "claude-sonnet-4-5-20250929")
    assert type(m).__name__ == "ChatAnthropic"
    assert m.max_tokens == 20_000
    assert m.max_retries == 5


def test_google_kwargs_pinned():
    m = build_chat_model("google", "gemini-3-pro-preview")
    assert type(m).__name__ == "ChatGoogleGenerativeAI"
    assert m.max_retries == 5


def test_ollama_kwargs_pinned(monkeypatch):
    """Ollama goes through its OpenAI-compatible endpoint with the OpenAI client."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    m = build_chat_model("ollama", "llama3")
    assert type(m).__name__ == "ChatOpenAI"
    assert m.openai_api_base == "http://localhost:11434/v1"
    # Streaming is enabled so the agent loop's astream emits tokens as they're
    # generated (perceived-latency win) instead of buffering the whole response.
    assert m.disable_streaming is False
    # Usage is only streamed back on request, and not requested by default for
    # a custom base URL: without this every turn would report zero tokens.
    assert m.stream_usage is True
    # The reply cap must travel as a raw `max_tokens`. The client's own field is
    # renamed to `max_completion_tokens`, which Ollama accepts and ignores (a
    # limit of 25 produced an 800-token reply against a live server).
    assert m.extra_body == {"max_tokens": 16384}
    assert m.max_tokens is None
    payload = m._get_request_payload([("user", "hi")])
    assert "max_completion_tokens" not in payload


def test_ollama_address_comes_from_ollama_host(monkeypatch):
    from novacode_cli.config.model_create import ollama_openai_base_url

    for host, expected in (
        ("127.0.0.1:11434", "http://127.0.0.1:11434/v1"),  # the CLI's scheme-less form
        ("0.0.0.0:11434", "http://localhost:11434/v1"),  # a bind address, not a destination
        ("http://box:11434/", "http://box:11434/v1"),
        ("https://ollama.com/v1", "https://ollama.com/v1"),
    ):
        monkeypatch.setenv("OLLAMA_HOST", host)
        assert ollama_openai_base_url() == expected


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        build_chat_model("nonsense", "x")


def test_from_config_returns_none_without_key(monkeypatch):
    # "No key" means neither env NOR keychain. Deleting the env var alone left a
    # developer's real keyring entry visible, so the gate passed and the client
    # raised instead of returning None.
    from novacode_cli.config import model_create

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(model_create.settings, "openai_api_key", None, raising=False)
    assert create_model_from_config("openai", "gpt-5-mini") is None


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-5-mini"),
    ("anthropic", "claude-sonnet-4-5-20250929"),
    ("ollama", "llama3"),
])
def test_model_manager_path_matches_direct_path(provider, model):
    """The drift that motivated this refactor: ModelManager's copies had lost
    max_retries/effort. Both paths must now construct identically."""
    from novacode_cli.config.model_manager import ModelManager

    direct = create_model_from_config(provider, model)
    managed = ModelManager().create_model_for_provider(provider, model)
    assert type(direct) is type(managed)
    for attr in ("max_retries", "max_tokens", "extra_body", "openai_api_base"):
        assert getattr(direct, attr, None) == getattr(managed, attr, None), attr


# ── async-agent graphs: provider-agnostic model resolution ─────────────────────
#
# The six remote graphs used to build ``ChatOllama`` themselves, so "which model
# runs my background research" was an Ollama question. These pin the resolution
# order and the three failure messages, which is the whole contract callers rely on.

_FALLBACK_VARS = ("ASYNC_AGENT_MODEL", "DOC_AGENT_MODEL", "PLAN_SCOUT_MODEL")


def _clear(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_async_agent_defaults_to_ollama_and_its_historical_model(monkeypatch):
    _clear(monkeypatch, "ASYNC_AGENT_PROVIDER", *_FALLBACK_VARS)
    model = build_async_agent_model()
    assert type(model).__name__ == "ChatOpenAI"  # Ollama's OpenAI-compatible endpoint
    assert model.openai_api_base.endswith("/v1")
    assert model.model_name == "gemma4:31b-cloud"


def test_async_agent_model_beats_doc_agent_model(monkeypatch):
    _clear(monkeypatch, "ASYNC_AGENT_PROVIDER")
    monkeypatch.setenv("DOC_AGENT_MODEL", "from-the-legacy-var")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "from-the-new-var")
    assert build_async_agent_model().model == "from-the-new-var"


def test_async_agent_still_honours_doc_agent_model(monkeypatch):
    _clear(monkeypatch, "ASYNC_AGENT_PROVIDER", "ASYNC_AGENT_MODEL", "PLAN_SCOUT_MODEL")
    monkeypatch.setenv("DOC_AGENT_MODEL", "legacy-model")
    assert build_async_agent_model().model == "legacy-model"


def test_per_agent_variable_wins(monkeypatch):
    _clear(monkeypatch, "ASYNC_AGENT_PROVIDER")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "shared-model")
    monkeypatch.setenv("PLAN_SCOUT_MODEL", "scout-only-model")
    model = build_async_agent_model(per_agent_model_var="PLAN_SCOUT_MODEL")
    assert model.model == "scout-only-model"


def test_async_agent_runs_on_a_non_ollama_provider(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "gpt-5-mini")
    model = build_async_agent_model()
    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "gpt-5-mini"
    assert model.max_retries == 5  # same constructor, same kwargs as the main agent


def test_unknown_async_agent_provider_names_the_variable(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "nonsense")
    with pytest.raises(ValueError, match="ASYNC_AGENT_PROVIDER"):
        build_async_agent_model()


def test_async_agent_missing_key_names_its_key_variable(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "claude-sonnet-4-5-20250929")
    _clear(monkeypatch, "ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_async_agent_model()


def test_provider_without_a_known_default_asks_for_a_model(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "nvidia")
    _clear(monkeypatch, *_FALLBACK_VARS)
    with pytest.raises(ValueError, match="ASYNC_AGENT_MODEL"):
        build_async_agent_model()
