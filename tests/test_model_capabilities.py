"""Tests for the static model-capability registry (multimodal detection)."""

from __future__ import annotations

import pytest

from novacode_cli.config.model_capabilities import (
    MULTIMODAL_MODEL_PATTERNS,
    model_supports_images,
)


class TestModelSupportsImages:
    @pytest.mark.parametrize(
        "model",
        [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "claude-3-5-sonnet-20241022",
            "claude-sonnet-4-5",
            "gemini-2.0-flash-exp",
            "qwen3-vl:235b-cloud",
            "llava:13b",
            "minicpm-v4.6:1b",
            "gemma4:31b-cloud",
            "deepseek-v4.1-flash:cloud",
            "pixtral-12b",
        ],
    )
    def test_known_multimodal_models(self, model: str):
        assert model_supports_images("ollama", model) is True

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-3.5-turbo",
            "qwen3-coder",
            "deepseek-v3.1:671b-cloud",
            "llama-3.1-70b",
            "mistral-large-3:675b-cloud",
            "glm-4.6",
        ],
    )
    def test_text_only_models(self, model: str):
        assert model_supports_images("ollama", model) is False

    def test_case_insensitive(self):
        assert model_supports_images("openai", "GPT-4O") is True

    def test_empty_model_name(self):
        assert model_supports_images("ollama", "") is False

    def test_override_true_wins_over_pattern(self):
        # An unlisted model can be declared multimodal explicitly.
        assert model_supports_images("ollama", "my-custom-model", override=True) is True

    def test_override_false_wins_over_pattern(self):
        # A pattern match can be overridden to text-only.
        assert model_supports_images("openai", "gpt-4o", override=False) is False

    def test_override_none_falls_back_to_pattern(self):
        assert model_supports_images("openai", "gpt-4o", override=None) is True
        assert model_supports_images("openai", "gpt-3.5-turbo", override=None) is False


class TestPatterns:
    def test_patterns_are_lowercase(self):
        # Matching is case-insensitive against a lowercased name, so a pattern
        # with uppercase would never match.
        assert all(p == p.lower() for p in MULTIMODAL_MODEL_PATTERNS)

    def test_qwen3_vl_is_specific_not_bare_qwen3(self):
        # A bare "qwen3" pattern would mark text-only qwen3-coder multimodal.
        assert "qwen3" not in MULTIMODAL_MODEL_PATTERNS
        assert "qwen3-vl" in MULTIMODAL_MODEL_PATTERNS
