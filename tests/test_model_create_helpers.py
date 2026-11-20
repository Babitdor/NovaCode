"""Pure helpers in model_create: reasoning kwargs, the /effort note, vision picks.

These functions never build a client, so they need no fake SDK: they decide
*which* keyword arguments a provider receives. That is where this module has
shipped real bugs — an effort setting silently dropped because the model was
not recognised, or ``off`` sent as something the model rejects. Every branch
here is a provider-specific promise, so each is pinned by value, not truthiness.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novacode_cli.config import model_create
from novacode_cli.config.model_create import (
    _claude_takes_adaptive_thinking,
    anthropic_thinking_kwargs,
    gateway_reasoning_kwargs,
    get_current_model_name,
    get_vision_model_suggestion,
    google_thinking_kwargs,
    model_supports_vision,
    openai_reasoning_kwargs,
    reasoning_setting_note,
)

# --- which Claude models think adaptively ------------------------------------


@pytest.mark.parametrize(
    ("model_name", "adaptive"),
    [
        ("claude-opus-4-6", True),
        ("claude-sonnet-4-6-20260101", True),
        ("claude-opus-4-7", True),
        ("claude-sonnet-5", True),
        ("claude-fable-5-1", True),
        ("claude-mythos-1", True),
        ("claude-sonnet-4-5-20250929", False),
        ("claude-haiku-4-5", False),
        ("claude-3-5-sonnet-20241022", False),
        ("gpt-5-mini", False),
    ],
)
def test_adaptive_thinking_is_decided_by_version(model_name: str, adaptive: bool) -> None:
    """The wrong answer sends either a rejected ``budget_tokens`` or a bare effort."""
    assert _claude_takes_adaptive_thinking(model_name) is adaptive


# --- Anthropic ---------------------------------------------------------------


def test_adaptive_claude_never_gets_thinking_disabled() -> None:
    """Opus 5.5 / Sonnet 5.5 / Fable reject disabled thinking, so "off" is minimum effort."""
    assert anthropic_thinking_kwargs("claude-opus-4-7", "off") == {"effort": "low"}


def test_adaptive_claude_with_default_effort_sends_nothing() -> None:
    assert anthropic_thinking_kwargs("claude-opus-4-7", None) == {}
    assert anthropic_thinking_kwargs("claude-opus-4-7", "default") == {}


def test_adaptive_claude_asks_for_a_summary_from_4_7_on() -> None:
    """Nova renders reasoning as its own stream; without display text it is empty."""
    assert anthropic_thinking_kwargs("claude-opus-4-7", "high") == {
        "effort": "high",
        "thinking": {"type": "adaptive", "display": "summarized"},
    }


def test_4_6_does_not_receive_the_display_field() -> None:
    """``display`` arrived with 4.7; older adaptive models summarize by default."""
    assert anthropic_thinking_kwargs("claude-opus-4-6", "high") == {
        "effort": "high",
        "thinking": {"type": "adaptive"},
    }


@pytest.mark.parametrize(("effort", "budget"), [("low", 2048), ("medium", 4096), ("high", 16384)])
def test_older_claude_gets_a_budget_per_effort(effort: str, budget: int) -> None:
    assert anthropic_thinking_kwargs("claude-sonnet-4-5-20250929", effort) == {
        "thinking": {"type": "enabled", "budget_tokens": budget}
    }


def test_a_configured_budget_beats_the_per_effort_default() -> None:
    assert anthropic_thinking_kwargs("claude-sonnet-4-5", "high", 1024) == {
        "thinking": {"type": "enabled", "budget_tokens": 1024}
    }


def test_older_claude_off_sends_no_thinking_block() -> None:
    assert anthropic_thinking_kwargs("claude-sonnet-4-5", "off") == {}


# --- OpenAI ------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name",
    ["gpt-4o", "gpt-4.1", "o1-mini", "o1-preview", "gpt-4o-chat", "gemini-3-pro"],
)
def test_models_without_reasoning_get_no_effort(model_name: str) -> None:
    """These reject ``reasoning_effort``; sending it fails the whole call."""
    assert openai_reasoning_kwargs(model_name, "high") == {}


def test_a_reasoning_model_gets_plain_effort() -> None:
    assert openai_reasoning_kwargs("o3", "high") == {"reasoning_effort": "high"}
    assert openai_reasoning_kwargs("gpt-5.1", "medium") == {"reasoning_effort": "medium"}


def test_openai_off_is_low_because_some_models_cannot_disable_thinking() -> None:
    assert openai_reasoning_kwargs("gpt-5", "off") == {"reasoning_effort": "low"}


def test_a_gateway_prefix_still_resolves_the_model_family() -> None:
    assert openai_reasoning_kwargs("openai/gpt-5", "low") == {"reasoning_effort": "low"}


def test_summary_support_switches_to_the_responses_api() -> None:
    """Encrypted reasoning + summary is the only way to stream reasoning text."""
    assert openai_reasoning_kwargs("gpt-5", "medium", summary=True) == {
        "use_responses_api": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "medium", "summary": "auto"},
    }


def test_o1_keeps_plain_effort_even_with_summary_requested() -> None:
    assert openai_reasoning_kwargs("o1", "medium", summary=True) == {"reasoning_effort": "medium"}


def test_openai_default_effort_sends_nothing() -> None:
    assert openai_reasoning_kwargs("gpt-5", None) == {}


# --- Gemini ------------------------------------------------------------------


def test_gemini_2_0_has_no_thinking_control() -> None:
    assert google_thinking_kwargs("gemini-2.0-flash", "high") == {}


def test_gemini_3_uses_a_thinking_level_and_keeps_thoughts() -> None:
    assert google_thinking_kwargs("gemini-3-flash", "medium") == {
        "thinking_level": "medium",
        "include_thoughts": True,
    }


def test_gemini_3_pro_cannot_be_asked_for_medium() -> None:
    """3 Pro takes only low/high; "medium" is a request error, not a preference."""
    assert google_thinking_kwargs("gemini-3-pro-preview", "medium")["thinking_level"] == "high"


def test_gemini_3_off_is_the_lowest_level() -> None:
    assert google_thinking_kwargs("gemini-3-pro-preview", "off")["thinking_level"] == "low"


@pytest.mark.parametrize(
    ("model_name", "budget"),
    [
        ("gemini-2.5-flash", 0),
        ("gemini-2.5-pro", 128),  # Pro has a floor, not a zero
        ("gemini-2.5-flash", 24576),
        ("gemini-2.5-pro", 32768),
    ],
)
def test_gemini_2_5_budgets(model_name: str, budget: int) -> None:
    effort = "off" if budget in {0, 128} else "high"

    kwargs = google_thinking_kwargs(model_name, effort)

    assert kwargs["thinking_budget"] == budget


def test_a_non_gemini_model_gets_no_thinking_control() -> None:
    assert google_thinking_kwargs("claude-opus-4-7", "high") == {}
    assert google_thinking_kwargs("gemini-3-pro-preview", None) == {}


# --- gateways ----------------------------------------------------------------


def test_openrouter_gets_its_unified_reasoning_field() -> None:
    assert gateway_reasoning_kwargs("openrouter", "z-ai/glm-5.2", "high") == {
        "extra_body": {"reasoning": {"effort": "high"}}
    }
    assert gateway_reasoning_kwargs("openrouter", "z-ai/glm-5.2", "off") == {
        "extra_body": {"reasoning": {"effort": "low"}}
    }


@pytest.mark.parametrize(
    ("effort", "expected"), [("off", "low"), ("low", "low"), ("medium", "high"), ("high", "high")]
)
def test_deepseek_v4_on_a_gateway_gets_two_levels(effort: str, expected: str) -> None:
    assert gateway_reasoning_kwargs("commandcode", "deepseek/deepseek-v4-flash", effort) == {
        "reasoning_effort": expected
    }


def test_a_gateway_passes_openai_shaped_effort_through() -> None:
    assert gateway_reasoning_kwargs("opencode", "gpt-5-mini", "high") == {
        "reasoning_effort": "high"
    }


def test_an_unknown_family_keeps_provider_defaults() -> None:
    """A guessed field is a request error; silence is the safe answer."""
    assert gateway_reasoning_kwargs("commandcode", "some-unknown-model", "high") == {}
    assert gateway_reasoning_kwargs("openrouter", "z-ai/glm-5.2", None) == {}


# --- the /effort note --------------------------------------------------------


def test_the_note_says_default_when_every_setting_is_default() -> None:
    expected = "Using the provider's default reasoning settings."
    assert reasoning_setting_note("openai", "gpt-5", None) == expected
    assert reasoning_setting_note("openai", "gpt-5", "default") == expected


@pytest.mark.parametrize(
    ("provider", "model_name", "effort", "fragment"),
    [
        ("nvidia", "deepseek-ai/deepseek-v4-pro-0813", "high", "NVIDIA"),
        ("ollama", "gemma4:31b-cloud", "medium", "Ollama"),
        ("google", "gemini-3-pro-preview", "high", "Gemini"),
        ("anthropic", "claude-sonnet-4-5-20250929", "high", "Claude"),
        ("commandcode", "anthropic/claude-sonnet-4.5", "high", "Claude"),
        ("opencode", "claude-opus-5", "high", "Claude"),
        ("openrouter", "z-ai/glm-5.2", "high", "OpenRouter"),
        ("openai", "gpt-5", "high", "reasoning effort"),
        ("commandcode", "deepseek/deepseek-v4-flash", "medium", "DeepSeek V4"),
    ],
)
def test_the_note_names_the_control_that_actually_applies(
    provider: str, model_name: str, effort: str, fragment: str
) -> None:
    """The note is the only place a user learns which knob their model honours."""
    assert fragment in reasoning_setting_note(provider, model_name, effort)


def test_an_unmapped_route_admits_it_has_no_control() -> None:
    note = reasoning_setting_note("mistral", "mistral-large-latest", "high")

    assert "No verified configurable effort control" in note


def test_a_claude_model_on_a_non_claude_route_does_not_claim_claude_control() -> None:
    """The route matters: an Anthropic-shaped claim on OpenAI would be wrong."""
    note = reasoning_setting_note("openai", "claude-sonnet-4-5", "high")

    assert "Claude receives" not in note


def test_gemini_without_a_thinking_control_falls_through_to_the_generic_note() -> None:
    """2.0 has no thinking, so claiming Gemini control would be a lie."""
    note = reasoning_setting_note("google", "gemini-2.0-flash", "high")

    assert "Gemini receives" not in note


# --- vision capability -------------------------------------------------------


def test_a_registry_model_is_answered_by_the_registry() -> None:
    assert model_supports_vision("gpt-4o") is True
    assert model_supports_vision("gpt-4o-mini") is True
    # Case-insensitive: the registry is also probed lowercased.
    assert model_supports_vision("GPT-4O") is True


def test_an_unknown_model_falls_back_to_name_keywords() -> None:
    assert model_supports_vision("qwen2.5-vl:72b") is True
    assert model_supports_vision("my-vision-model") is True
    assert model_supports_vision("gpt-5-mini") is False


def test_a_vision_model_gets_no_suggestion() -> None:
    assert get_vision_model_suggestion("gpt-4o") is None


@pytest.mark.parametrize(
    ("flags", "suggested"),
    [
        ({"has_anthropic": True}, "claude-sonnet-4-5-20250929"),
        ({"has_openai": True}, "gpt-4o"),
        ({"has_google": True}, "gemini-1.5-pro"),
        ({}, "qwen3-vl:235b-cloud"),
    ],
)
def test_the_suggestion_follows_the_configured_provider(monkeypatch, flags, suggested: str) -> None:
    """Suggesting a model on a provider the user has no key for is a dead end."""
    monkeypatch.setattr(
        model_create,
        "settings",
        SimpleNamespace(
            **{"has_anthropic": False, "has_openai": False, "has_google": False, **flags}
        ),
    )

    assert get_vision_model_suggestion("gpt-5-mini") == suggested


# --- current model name ------------------------------------------------------


def test_a_saved_model_name_wins(monkeypatch) -> None:
    class _Saved:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_model_config(self) -> dict[str, str]:
            return {"provider": "openai", "model": "gpt-6-sol"}

    monkeypatch.setattr(model_create, "settings", SimpleNamespace(has_openai=True))
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _Saved)

    assert get_current_model_name() == "gpt-6-sol"


@pytest.mark.parametrize(
    ("flag", "env_var", "default"),
    [
        ("has_openai", "OPENAI_MODEL", "gpt-5-mini"),
        ("has_anthropic", "ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        ("has_google", "GOOGLE_MODEL", "gemini-3-pro-preview"),
    ],
)
def test_an_environment_override_beats_the_default(
    monkeypatch, flag: str, env_var: str, default: str
) -> None:
    class _None:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_model_config(self) -> None:
            return None

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _None)
    monkeypatch.setattr(
        model_create,
        "settings",
        SimpleNamespace(
            **{"has_openai": False, "has_anthropic": False, "has_google": False, flag: True}
        ),
    )
    monkeypatch.delenv(env_var, raising=False)

    assert get_current_model_name() == default

    monkeypatch.setenv(env_var, "pinned-model")
    assert get_current_model_name() == "pinned-model"


def test_without_any_key_the_ollama_model_is_current(monkeypatch) -> None:
    class _None:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_model_config(self) -> None:
            return None

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _None)
    monkeypatch.setattr(
        model_create,
        "settings",
        SimpleNamespace(has_openai=False, has_anthropic=False, has_google=False),
    )
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:31b-cloud")

    assert get_current_model_name() == "gemma4:31b-cloud"
