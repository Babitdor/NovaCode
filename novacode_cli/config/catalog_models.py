"""Construct catalog providers through Nova's installed chat SDKs.

The reviewed snapshot chooses the wire protocol. A provider name is never
treated as proof that every model uses OpenAI Chat Completions.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from novacode_cli.config.provider_registry import CATALOG_PROVIDERS, missing_provider_settings

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


def build_catalog_model(provider: str, model_name: str) -> BaseChatModel:
    """Build exactly the requested provider, without fallback or network calls.

    Raises:
        ValueError: The provider, model capability, key, or endpoint is unavailable.
    """
    from novacode_cli.config.model_manager import provider_api_key, provider_base_url

    preset = CATALOG_PROVIDERS[provider]
    if reason := preset.get("unavailable_reason"):
        message = f"{preset['name']}: {reason}"
        raise ValueError(message)
    if not model_name.strip():
        message = f"Choose a current model ID for {preset['name']}"
        raise ValueError(message)
    if missing := missing_provider_settings(provider, model_name):
        message = f"{preset['name']} requires settings: {', '.join(missing)}"
        raise ValueError(message)
    key = provider_api_key(provider) if preset.get("api_key_var") else None
    if preset["requires_api_key"] and not key:
        message = f"{preset['name']} requires {preset['api_key_var']}"
        raise ValueError(message)

    override = preset.get("model_overrides", {}).get(model_name, {})
    protocol = override.get("protocol", preset["protocol"])
    if protocol == "vertex":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            vertexai=True,
            project=os.environ["GOOGLE_VERTEX_PROJECT"],
            location=os.environ.get("GOOGLE_VERTEX_LOCATION", "global"),
            max_retries=5,
        )
    if protocol in {"gitlab", "watsonx", "sap"} or preset["protocol"] == "gitlab":
        from novacode_cli.config.provider_transports import build_managed_model

        return build_managed_model(provider, model_name, key or "", override)

    base_url = provider_base_url(provider, model_name)

    if protocol == "messages":
        from langchain_anthropic import ChatAnthropic
        from pydantic import SecretStr

        return ChatAnthropic(
            model_name=model_name,
            api_key=SecretStr(key or ""),
            base_url=base_url.removesuffix("/v1"),
            max_tokens_to_sample=16_384,
            max_retries=5,
            timeout=300,
            stop=None,
        )
    if protocol not in {"chat", "responses"}:
        message = f"Unsupported protocol for {preset['name']}: {protocol}"
        raise ValueError(message)

    from novacode_cli.config.model_create import _opencode_chat_class
    from novacode_cli.utils.backend_patches import apply_openai_reasoning_content_patch

    apply_openai_reasoning_content_patch()
    headers = (
        {"cf-aig-authorization": f"Bearer {key}"} if provider == "cloudflare-ai-gateway" else {}
    )
    return _opencode_chat_class()(
        model=model_name,
        api_key=key or "nova-local",
        base_url=base_url,
        max_retries=5,
        # Usage chunks are optional extensions, not part of the chat contract.
        stream_usage=False,
        use_responses_api=protocol == "responses",
        default_headers=headers,
    )
