"""Reasoning settings per provider: Claude adaptive + effort, OpenAI reasoning_effort, Gemini level or budget."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli.config.model_create import anthropic_thinking_kwargs


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-fable-5-1", "claude-haiku-5-5",
        "anthropic/claude-sonnet-5-5",
    ],
)
def test_current_models_get_adaptive_thinking_and_effort_never_a_budget(model):
    kwargs = anthropic_thinking_kwargs(model, "medium", budget=8000)
    assert kwargs == {"effort": "medium", "thinking": {"type": "adaptive", "display": "summarized"}}
    assert anthropic_thinking_kwargs(model, "off") == {"effort": "low"}, "disabled thinking is a 400"
    assert anthropic_thinking_kwargs(model, "default") == {}


def test_4_6_models_are_adaptive_without_display():
    assert anthropic_thinking_kwargs("claude-sonnet-4-6", "high") == {
        "effort": "high", "thinking": {"type": "adaptive"}
    }


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4-20250514", "claude-3-7-sonnet-latest"]
)
def test_older_models_keep_budget_tokens(model):
    assert anthropic_thinking_kwargs(model, "high") == {"thinking": {"type": "enabled", "budget_tokens": 16384}}
    assert anthropic_thinking_kwargs(model, "off") == {}
    assert anthropic_thinking_kwargs(model, "off", budget=3000)["thinking"]["budget_tokens"] == 3000


def test_the_request_langchain_builds_has_no_budget_for_sonnet_5_5():
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(
        model_name="claude-sonnet-5-5", api_key="test", max_tokens=20_000,
        **anthropic_thinking_kwargs("claude-sonnet-5-5", "high"),
    )
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "high"}
    assert "budget_tokens" not in str(payload)


# -- OpenAI ------------------------------------------------------------------

from novacode_cli.config.model_create import google_thinking_kwargs, openai_reasoning_kwargs  # noqa: E402


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-6-astra", "gpt-6.1-sol", "o4-mini", "o3", "openai/gpt-5.4"])
def test_openai_reasoning_models_get_an_effort_and_off_is_low(model):
    assert openai_reasoning_kwargs(model, "high") == {"reasoning_effort": "high"}
    assert openai_reasoning_kwargs(model, "off") == {"reasoning_effort": "low"}, "none/minimal 400 on some"
    assert openai_reasoning_kwargs(model, None) == {}


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1-mini", "gpt-5-chat-latest", "o1-mini", "deepseek-chat"])
def test_openai_models_without_reasoning_get_nothing(model):
    assert openai_reasoning_kwargs(model, "high") == {}


def test_supported_responses_models_request_visible_summaries_only_when_enabled():
    assert openai_reasoning_kwargs("gpt-5.5", "high", summary=True) == {
        "use_responses_api": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "high", "summary": "auto"},
    }
    assert openai_reasoning_kwargs("gpt-4o", "high", summary=True) == {}
    assert openai_reasoning_kwargs("o1", "high", summary=True) == {"reasoning_effort": "high"}
    assert openai_reasoning_kwargs("gpt-5.5", "off", summary=True) == {"reasoning_effort": "low"}


@pytest.mark.parametrize(("responses_api", "key"), [(False, "reasoning_effort"), (True, "reasoning")])
def test_the_openai_request_carries_the_effort_for_either_api(responses_api, key):
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model="gpt-5.5", api_key="test", use_responses_api=responses_api,
        **openai_reasoning_kwargs("gpt-5.5", "medium"),
    )
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload[key] == ("medium" if key == "reasoning_effort" else {"effort": "medium"})


# -- Google ------------------------------------------------------------------


@pytest.mark.parametrize("model", ["gemini-3.1-pro-preview", "gemini-3.6-flash", "gemini-4-argon"])
def test_gemini_3_and_later_take_a_thinking_level(model):
    assert google_thinking_kwargs(model, "medium") == {"thinking_level": "medium", "include_thoughts": True}
    assert google_thinking_kwargs(model, "off")["thinking_level"] == "low"
    assert "thinking_budget" not in google_thinking_kwargs(model, "high")


def test_gemini_3_pro_has_no_medium():
    assert google_thinking_kwargs("gemini-3-pro-preview", "medium")["thinking_level"] == "high"


def test_gemini_2_5_takes_a_budget_within_its_limits_and_2_0_nothing():
    assert google_thinking_kwargs("gemini-2.5-flash", "high")["thinking_budget"] == 24576
    assert google_thinking_kwargs("gemini-2.5-pro", "high")["thinking_budget"] == 32768
    assert google_thinking_kwargs("gemini-2.5-flash", "off") == {"thinking_budget": 0}
    assert google_thinking_kwargs("gemini-2.5-pro", "off") == {"thinking_budget": 128}, "Pro cannot turn it off"
    assert "thinking_level" not in google_thinking_kwargs("gemini-2.5-flash", "low")
    assert google_thinking_kwargs("gemini-2.0-flash", "high") == {}


def test_the_gemini_request_carries_only_the_level():
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = ChatGoogleGenerativeAI(
        model="gemini-3.1-pro-preview", google_api_key="test",
        **google_thinking_kwargs("gemini-3.1-pro-preview", "high"),
    )
    thinking = model._prepare_params(None).thinking_config
    assert "high" in str(thinking.thinking_level).lower()
    assert thinking.thinking_budget is None and thinking.include_thoughts is True
