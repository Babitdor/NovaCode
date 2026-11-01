"""Explicit public-provider capabilities for conversation and deferred tools."""

from __future__ import annotations

# This directory is an intentional namespace package.
# ruff: noqa: INP001
import re
from urllib.parse import urlparse

# Routes enter this set only after payload, permission, live quality and cost checks pass.
VERIFIED_NATIVE_ROUTES: frozenset[tuple[str, str]] = frozenset()


def public_provider(model: object) -> str:
    """Require Nova route identity and a known adapter before adding native fields."""
    metadata = getattr(model, "metadata", None) or {}
    provider = metadata.get("nova_provider")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        endpoint = getattr(model, "anthropic_api_url", None)
        if isinstance(model, ChatAnthropic) and (
            not endpoint or urlparse(str(endpoint)).hostname == "api.anthropic.com"
        ):
            return "anthropic"
    elif provider == "openai":
        from langchain_openai import ChatOpenAI

        endpoint = getattr(model, "openai_api_base", None)
        if isinstance(model, ChatOpenAI) and (
            not endpoint or urlparse(str(endpoint)).hostname == "api.openai.com"
        ):
            return "openai"
    return ""


def native_tool_search(model: object) -> str:
    """Verified model routes with installed native search and deferred-tool support."""
    provider = public_provider(model)
    metadata = getattr(model, "metadata", None) or {}
    name = str(metadata.get("nova_model") or getattr(model, "model_name", "")).lower()
    if (provider, name) not in VERIFIED_NATIVE_ROUTES:
        return ""
    api_key = getattr(model, "openai_api_key", None)
    if provider == "anthropic" and re.match(
        r"claude-(?:opus|sonnet|haiku)-4[-.](?:5|6)(?:[-.]|$)", name
    ):
        return provider
    if (
        provider == "openai"
        and getattr(model, "use_responses_api", False)
        and re.match(r"gpt-5\.(?:4|5)(?:-|$)", name)
        and api_key
        and api_key.get_secret_value() != "chatgpt-plan-token-managed-per-request"
    ):
        return provider
    return ""


def unsupported_native_feature(error: Exception) -> bool:
    """Retry only explicit feature rejection, never authentication or policy errors."""
    if getattr(error, "status_code", None) not in {400, 422}:
        return False
    text = str(error).lower()
    if any(
        reason in text
        for reason in (
            "permission",
            "unauthorized",
            "authentication",
            "api key",
            "access denied",
            "unknown model",
            "model not found",
            "model_not_found",
            "billing",
            "quota",
            "credit",
        )
    ):
        return False
    field = r"(?:defer_loading|tool_search|cache_control)"
    rejection = r"(?:unsupported|not supported|unknown|unrecognized)"
    return bool(
        re.search(
            rf"{field}['\"]?\s+(?:is\s+)?{rejection}\b|"
            rf"{rejection}\s+(?:(?:parameter|field|feature|tool)[:\s]+)?['\"]?{field}\b",
            text,
        )
    )


def breakpoint_count(value: object) -> int:
    """Count explicit breakpoints without retaining any message content."""
    if isinstance(value, dict):
        return int("cache_control" in value) + sum(breakpoint_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(breakpoint_count(v) for v in value)
    return 0
