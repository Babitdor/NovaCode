"""Model provider management for Nova CLI.

Handles switching between different LLM providers (OpenAI, Anthropic, Ollama,
Google, OpenRouter) during interactive sessions.
"""

import os
import subprocess
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel

from novacode_cli.config.config import Settings, console
from novacode_cli.config.nova_config import NovaConfig

# OpenRouter is OpenAI-API-compatible; routed through ChatOpenAI with this base URL.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenCode Go is OpenCode's subscription model gateway, OpenAI-API-compatible.
# Routed through ChatOpenAI with this base URL (same pattern as OpenRouter).
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"

# Type for supported providers
ProviderType = Literal[
    "openai", "anthropic", "ollama", "google", "openrouter", "opencode", "nvidia"
]


# Model provider presets
#: The model Nova falls back to when nothing else is chosen: no saved /model
#: pick, or the saved provider has no API key.
DEFAULT_OLLAMA_MODEL = "deepseek-v4.1-flash:cloud"

MODEL_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "description": "OpenAI GPT models (gpt-4, gpt-4-turbo, gpt-5-mini, etc.)",
        "default_model": "gpt-5-mini",
        "env_var": "OPENAI_MODEL",
        "api_key_var": "OPENAI_API_KEY",
        "requires_api_key": True,
        "models": [
            "gpt-5-mini",
            "gpt-4-turbo",
            "gpt-4",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-3.5-turbo",
        ],
    },
    "anthropic": {
        "name": "Anthropic",
        "description": "Claude models (claude-sonnet, claude-opus, etc.)",
        "default_model": "claude-sonnet-4-5-20250929",
        "env_var": "ANTHROPIC_MODEL",
        "api_key_var": "ANTHROPIC_API_KEY",
        "requires_api_key": True,
        "models": [
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-5-20251101",
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
        ],
    },
    "ollama": {
        "name": "Ollama",
        "description": "Local Ollama models (qwen, llama, mistral, etc.)",
        "default_model": DEFAULT_OLLAMA_MODEL,
        "env_var": "OLLAMA_MODEL",
        "api_key_var": None,
        "requires_api_key": False,
        "models": [
            DEFAULT_OLLAMA_MODEL,
            "qwen3-coder:480b-cloud",
            "qwen2.5:72b",
            "llama3.3:70b",
            "deepseek-r1:70b",
            "mistral",
            "codestral",
        ],
    },
    "google": {
        "name": "Google",
        "description": "Google Gemini models",
        "default_model": "gemini-3-pro-preview",
        "env_var": "GOOGLE_MODEL",
        "api_key_var": "GOOGLE_API_KEY",
        "requires_api_key": True,
        "models": [
            "gemini-3-pro-preview",
            "gemini-2.0-flash-exp",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ],
    },
    "openrouter": {
        "name": "OpenRouter",
        "description": "Unified access to many models (Anthropic, OpenAI, Llama, etc.)",
        "default_model": "anthropic/claude-3.5-sonnet",
        "env_var": "OPENROUTER_MODEL",
        "api_key_var": "OPENROUTER_API_KEY",
        "requires_api_key": True,
        "base_url": OPENROUTER_BASE_URL,
        "models": [
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "google/gemini-2.0-flash-exp",
            "meta-llama/llama-3.3-70b-instruct",
            "deepseek/deepseek-chat",
        ],
    },
    "opencode": {
        "name": "OpenCode Go",
        "description": "OpenCode's subscription gateway (OpenAI-compatible chat models: GLM, Kimi, DeepSeek, MiMo, Hy3, Ox)",
        "default_model": "glm-5.3",
        "env_var": "OPENCODE_MODEL",
        "api_key_var": "OPENCODE_API_KEY",
        "requires_api_key": True,
        "base_url": OPENCODE_BASE_URL,
        # Only the models served via /chat/completions work through ChatOpenAI.
        # MiniMax/Qwen need the Anthropic Messages API and Grok/GPT-5.6/Muse
        # need the Responses API — those are NOT OpenAI-chat-compatible and are
        # intentionally excluded here (they'd fail with ChatOpenAI).
        "models": [
            "glm-5.3",
            "glm-5.2",
            "glm-5.1",
            "glm-5",
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.6",
            "kimi-k2.5",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "mimo-v2.5-pro",
            "mimo-v2.5",
            "mimo-v2-pro",
            "mimo-v2-omni",
            "hy3",
            "hy3-preview",
            "ox-alpha-free",
        ],
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "description": "NVIDIA-hosted models (DeepSeek, Nemotron, Llama) via build.nvidia.com",
        "default_model": "deepseek-ai/deepseek-v4-pro-0813",
        "env_var": "NVIDIA_MODEL",
        "api_key_var": "NVIDIA_API_KEY",
        "requires_api_key": True,
        # Verified callable against a live NVIDIA account. Two catalog
        # entries (llama-3.1-nemotron-70b-instruct, codestral-22b) were
        # dropped: they are listed by /v1/models but return 404 'Function
        # not found for account', so offering them only produces a
        # confusing failure at first use.
        #
        # The deepseek reasoning models are slow — a single structured
        # /council turn measured ~280s — so nemotron-3-super is the one
        # to pick for anything latency-sensitive.
        "models": [
            "deepseek-ai/deepseek-v4-pro-0813",
            "deepseek-ai/deepseek-v4-flash-0731",
            "nvidia/nemotron-3-super-120b-a12b",
            "openai/gpt-oss-120b",
        ],
    },
}


def get_ollama_models() -> list[str]:
    """Get list of available Ollama models by running 'ollama list'.

    Returns:
        List of model names, or fallback list if command fails
    """
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )

        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            models = []

            # Skip header line and parse model names
            for line in lines[1:]:
                if line.strip():
                    # Model name is the first column
                    model_name = line.split()[0]
                    models.append(model_name)

            if models:
                return models

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        # If ollama command fails, fall back to preset list
        pass

    # Fallback to preset models if command fails
    return MODEL_PRESETS["ollama"]["models"]


class ModelManager:
    """Manages model provider selection and switching."""

    def __init__(self):
        """Initialize model manager with current settings."""
        self.settings = Settings.from_environment()
        self.nova_config = NovaConfig()
        self.current_provider: ProviderType | None = None
        self.current_model: str | None = None

    def get_available_providers(self) -> list[tuple[str, dict[str, Any]]]:
        """Get list of available providers based on configured API keys.

        Returns:
            List of (provider_id, preset) tuples for available providers
        """
        available = []
        for provider_id, preset in MODEL_PRESETS.items():
            if not preset["requires_api_key"]:
                # Ollama is always available
                available.append((provider_id, preset))
            else:
                # Check if API key is configured
                api_key_var = preset["api_key_var"]
                if api_key_var and os.environ.get(api_key_var):
                    available.append((provider_id, preset))
        return available

    def get_current_provider(self) -> tuple[str, str] | None:
        """Get currently active provider and model.

        Checks saved config first, then falls back to environment variables.

        Returns:
            Tuple of (provider_name, model_name) or None
        """
        # Check saved configuration first
        saved_config = self.nova_config.get_model_config()
        if saved_config:
            provider_id = saved_config["provider"]
            model_name = saved_config["model"]
            preset = MODEL_PRESETS.get(provider_id)
            if preset:
                return (preset["name"], model_name)

        # Fall back to environment variables - check API keys in order
        if self.settings.has_openai:
            model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
            return ("OpenAI", model)
        if self.settings.has_anthropic:
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
            return ("Anthropic", model)
        if self.settings.has_google:
            model = os.environ.get("GOOGLE_MODEL", "gemini-3-pro-preview")
            return ("Google", model)
        if self.settings.has_openrouter:
            model = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
            return ("OpenRouter", model)
        if self.settings.has_opencode:
            model = os.environ.get("OPENCODE_MODEL", "glm-5.3")
            return ("OpenCode Go", model)
        if self.settings.has_nvidia:
            model = os.environ.get("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro-0813")
            return ("NVIDIA NIM", model)
        # Default to Ollama (always available, no API key needed)
        model = os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        return ("Ollama", model)

    def get_current_provider_id(self) -> str | None:
        """Preset key (e.g. 'openai') for the currently active provider.

        Reverse-maps the display name from get_current_provider(); the id is
        what set_provider and the UI pickers speak.
        """
        current = self.get_current_provider()
        if not current:
            return None
        for provider_id, preset in MODEL_PRESETS.items():
            if preset["name"] == current[0]:
                return provider_id
        return None

    def resolve_api_key(self, provider: str) -> str | None:
        """Existing API key for a provider, from the OS keychain or environment.

        Exports the key into os.environ when found so model construction can
        read it. Key Policy: what to do when this returns None (prompt, warn,
        or fail) belongs to the caller, never here.
        """
        preset = MODEL_PRESETS.get(provider)
        api_key_var = preset.get("api_key_var") if preset else None
        if not api_key_var:
            return None
        from novacode_cli.onboarding import SecretManager

        key = SecretManager().get_secret(api_key_var.lower()) or os.environ.get(
            api_key_var
        )
        if key:
            os.environ[api_key_var] = key
        return key

    def create_model_for_provider(
        self, provider: ProviderType, model_name: str | None = None
    ) -> BaseChatModel:
        """Create a model instance for the specified provider.

        Args:
            provider: Provider identifier (openai, anthropic, ollama, google)
            model_name: Specific model name (optional, uses default if not provided)

        Returns:
            BaseChatModel instance

        Raises:
            ValueError: If provider is invalid or API key is missing
        """
        preset = MODEL_PRESETS.get(provider)
        if not preset:
            raise ValueError(f"Unknown provider: {provider}")

        # Use provided model name or default
        if model_name is None:
            model_name = preset["default_model"]

        # Check API key requirement
        if preset["requires_api_key"]:
            api_key_var = preset["api_key_var"]
            if not os.environ.get(api_key_var):
                raise ValueError(f"{preset['name']} requires {api_key_var} environment variable")

        # Construction lives in ONE place (build_chat_model). This path used to
        # keep its own drifted copies — mid-session model switches silently
        # lost reasoning_effort / max_retries / thinking budgets vs. boot.
        from novacode_cli.config.model_create import build_chat_model

        return build_chat_model(provider, model_name)  # type: ignore[arg-type]

    def set_provider(
        self,
        provider: ProviderType,
        model_name: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Set the current provider and model.

        Saves configuration to Nova.config.json for persistence across sessions.

        Args:
            provider: Provider to use
            model_name: Model name (optional)
            base_url: Optional OpenAI-compatible endpoint override, for pointing
                the ``openai`` provider at Azure, LM Studio, vLLM or a proxy.
                Blank clears any previously saved override.
        """
        preset = MODEL_PRESETS.get(provider)
        if not preset:
            raise ValueError(f"Unknown provider: {provider}")

        # Use default model if not specified
        if model_name is None:
            model_name = preset["default_model"]

        # Save to persistent configuration
        self.nova_config.set_model_config(provider, model_name, base_url)

        # Also set environment variables for immediate effect in current session
        if preset["env_var"]:
            os.environ[preset["env_var"]] = model_name

        self.current_provider = provider
        self.current_model = model_name

        console.print(f"[dim]Switched to {preset['name']}: {self.current_model}[/dim]")
