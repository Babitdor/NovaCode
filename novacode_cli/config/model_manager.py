"""Model provider management for Nova CLI.

Handles switching between different LLM providers (OpenAI, Anthropic, Ollama,
Google, OpenRouter) during interactive sessions.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any, Literal

from novacode_cli.config.config import Settings, console
from novacode_cli.config.nova_config import NovaConfig

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from novacode_cli.config.provider_auth import ProviderAuthStatus

# OpenRouter is OpenAI-API-compatible; routed through ChatOpenAI with this base URL.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenCode Go is OpenCode's subscription model gateway, OpenAI-API-compatible.
# Routed through ChatOpenAI with this base URL (same pattern as OpenRouter).
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
# Command Code's Provider API (https://commandcode.ai/docs/provider).
COMMANDCODE_BASE_URL = "https://api.commandcode.ai/provider/v1"

# Type for supported providers
ProviderType = Literal[
    "openai", "anthropic", "ollama", "google", "openrouter", "opencode", "opencode_zen",
    "commandcode", "nvidia", "azure", "bedrock", "mistral", "groq", "cerebras", "xai",
    "huggingface", "kimi_coding", "minimax",
]

#: Providers that speak OpenAI Chat Completions at a fixed URL (``base_url`` in
#: their preset, overridable through ``base_url_env``). One client covers them all.
OPENAI_COMPATIBLE_PROVIDERS: frozenset[str] = frozenset(
    {"mistral", "groq", "cerebras", "xai", "huggingface", "kimi_coding"}
)

#: Providers that accept a custom OpenAI-compatible endpoint. OpenRouter and
#: OpenCode are also OpenAI-compatible but pin their own base URL, so offering
#: an endpoint there would only let the user break the gateway; Anthropic,
#: Google and Ollama take none at all.
#:
#: Shared by `/auth` (which owns the field) and `/model` (which applies the
#: stored value), so the two can never disagree about who accepts one.
ENDPOINT_PROVIDERS: frozenset[str] = frozenset({"openai", "azure"})


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
    "opencode_zen": {
        "name": "OpenCode Zen",
        "description": "OpenCode's pay-as-you-go gateway",
        "default_model": "big-pickle",
        "env_var": "OPENCODE_ZEN_MODEL",
        "api_key_var": "OPENCODE_ZEN_API_KEY",
        "requires_api_key": True,
        "base_url": OPENCODE_ZEN_BASE_URL,
        "models": ["big-pickle", "deepseek-v4.1-flash", "glm-5.3", "kimi-k3", "claude-sonnet-4-6", "gpt-5.4"],
    },
    "commandcode": {
        "name": "Command Code",
        "description": "Command Code's Provider API (Claude, GPT, DeepSeek, GLM, Kimi, Qwen...)",
        "default_model": "deepseek/deepseek-v4-flash",
        "env_var": "COMMANDCODE_MODEL",
        "api_key_var": "COMMANDCODE_API_KEY",
        "requires_api_key": True,
        "base_url": COMMANDCODE_BASE_URL,
        # Offline fallback only; the picker reads the live /models list.
        "models": [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "zai-org/GLM-5.3",
            "moonshotai/Kimi-K3",
            "claude-sonnet-5-5",
            "gpt-5.5",
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
    # The providers below list their tool-capable models from models.dev
    # ("models_dev" is its provider id), newest first, after these curated ids.
    # "key_aliases" are other variable names the same credential is read from.
    "azure": {
        "name": "Azure OpenAI",
        "description": "Your Azure OpenAI / AI Foundry deployments (v1 API)",
        "default_model": "gpt-6.1-sol",
        "env_var": "AZURE_OPENAI_MODEL",
        "api_key_var": "AZURE_OPENAI_API_KEY",
        "key_aliases": ("AZURE_API_KEY",),
        "requires_api_key": True,
        "models_dev": "azure",
        # Claude on Azure speaks the Anthropic API, not this provider's OpenAI v1 route.
        "models_dev_skip": ("claude-",),
        # Deployment names, which default to the model name in Azure.
        "models": ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"],
    },
    "bedrock": {
        "name": "Amazon Bedrock",
        "description": "Claude, GPT and open models on AWS (Bedrock API key or AWS credentials)",
        "default_model": "global.anthropic.claude-sonnet-5-5",
        "env_var": "BEDROCK_MODEL",
        "api_key_var": "AWS_BEARER_TOKEN_BEDROCK",
        # Any standard AWS credential works too; boto3 finds it itself.
        "key_aliases": ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"),
        "requires_api_key": True,
        "models_dev": "amazon-bedrock",
        "models": [
            "global.anthropic.claude-sonnet-5-5",
            "us.anthropic.claude-opus-5-5",
            "global.anthropic.claude-haiku-5-5",
            "global.openai.gpt-6.1-sol",
        ],
    },
    "mistral": {
        "name": "Mistral",
        "description": "Mistral AI models (Devstral, Mistral Large/Medium/Small)",
        "default_model": "devstral-latest",
        "env_var": "MISTRAL_MODEL",
        "api_key_var": "MISTRAL_API_KEY",
        "requires_api_key": True,
        "base_url": "https://api.mistral.ai/v1",
        "models_dev": "mistral",
        "models": [
            "devstral-latest",
            "mistral-large-4",
            "mistral-medium-latest",
            "mistral-small-latest",
        ],
    },
    "groq": {
        "name": "Groq",
        "description": "Fast inference on Groq LPUs (GPT-OSS, Qwen, Llama)",
        "default_model": "openai/gpt-oss-120b",
        "env_var": "GROQ_MODEL",
        "api_key_var": "GROQ_API_KEY",
        "requires_api_key": True,
        "base_url": "https://api.groq.com/openai/v1",
        "models_dev": "groq",
        "models": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile"],
    },
    "cerebras": {
        "name": "Cerebras",
        "description": "Fast inference on Cerebras (GPT-OSS, Qwen)",
        "default_model": "gpt-oss-120b",
        "env_var": "CEREBRAS_MODEL",
        "api_key_var": "CEREBRAS_API_KEY",
        "requires_api_key": True,
        "base_url": "https://api.cerebras.ai/v1",
        "models_dev": "cerebras",
        "models": ["gpt-oss-120b", "qwen-3.8-27b"],
    },
    "xai": {
        "name": "xAI",
        "description": "xAI Grok models",
        "default_model": "grok-4.7",
        "env_var": "XAI_MODEL",
        "api_key_var": "XAI_API_KEY",
        "requires_api_key": True,
        "base_url": "https://api.x.ai/v1",
        "models_dev": "xai",
        "models": ["grok-4.7", "grok-4.6", "grok-build-0.1", "grok-4.3"],
    },
    "huggingface": {
        "name": "Hugging Face",
        "description": "Hugging Face Inference Providers (DeepSeek, GLM, Kimi, Qwen...)",
        "default_model": "deepseek-ai/DeepSeek-V4.1-Flash",
        "env_var": "HF_MODEL",
        "api_key_var": "HF_TOKEN",
        "requires_api_key": True,
        "base_url": "https://router.huggingface.co/v1",
        "models_dev": "huggingface",
        "models": [
            "deepseek-ai/DeepSeek-V4.1-Flash",
            "zai-org/GLM-5.3",
            "moonshotai/Kimi-K3",
            "Qwen/Qwen3.8-27B",
        ],
    },
    "kimi_coding": {
        "name": "Kimi For Coding",
        # kimi.ai accounts: KIMI_BASE_URL=https://api.kimi.ai/coding/v1
        "description": "Kimi Code plan subscription (kimi.com / kimi.ai)",
        "default_model": "kimi-for-coding",
        "env_var": "KIMI_MODEL",
        "api_key_var": "KIMI_API_KEY",
        "requires_api_key": True,
        "base_url": "https://api.kimi.com/coding/v1",
        "base_url_env": "KIMI_BASE_URL",
        "models_dev": "kimi-code-plan-cn",
        "models": ["kimi-for-coding", "kimi-for-coding-highspeed", "k3"],
    },
    "minimax": {
        "name": "MiniMax",
        "description": "MiniMax models, pay-as-you-go or Token Plan (Anthropic-compatible API)",
        "default_model": "MiniMax-M3",
        "env_var": "MINIMAX_MODEL",
        "api_key_var": "MINIMAX_API_KEY",
        "requires_api_key": True,
        # The Anthropic SDK appends /v1/messages. MINIMAX_BASE_URL switches region
        # (https://api.minimax.cn/anthropic for minimax.cn accounts).
        "base_url": "https://api.minimax.io/anthropic",
        "base_url_env": "MINIMAX_BASE_URL",
        "models_dev": "minimax-coding-plan",
        "models": ["MiniMax-M3", "MiniMax-M3.1-Flash", "MiniMax-M2.7"],
    },
}


def provider_base_url(provider: str) -> str:
    """The provider's endpoint: its ``base_url_env`` override, else the preset's."""
    preset = MODEL_PRESETS[provider]
    override = os.environ.get(preset.get("base_url_env") or "", "").strip()
    return (override or preset["base_url"]).rstrip("/")


def provider_api_key(provider: str) -> str | None:
    """The provider's key: keychain, then environment, over every name it is read as."""
    from novacode_cli.config.credentials import get_stored_key

    preset = MODEL_PRESETS[provider]
    for var in (preset["api_key_var"], *preset.get("key_aliases", ())):
        if key := get_stored_key(var) or os.environ.get(var):
            return key
    return None


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


def get_opencode_models(provider: str = "opencode") -> list[str]:
    """Get the live list of conversational models from Go or Zen.

    Mirrors :func:`get_ollama_models`: ask the provider what it actually serves
    rather than trusting the hand-maintained preset list, which drifts (six of
    the preset's eighteen ids are already ``deprecated`` and 400 on first use).

    The model list can be public; retrieving it does not validate a key or
    confirm the account's entitlement to a particular model.

    Returns:
        Model ids sorted for a stable display, or the preset list if the request
        fails (offline, gateway down) — same best-effort contract as Ollama.
    """
    # Resolve keyring-or-env: a key saved to the system keychain is not in
    # os.environ on a fresh session, so reading the env alone would 401.
    from novacode_cli.config.opencode_gateway import headers as gateway_headers, protocol
    from novacode_cli.config.model_create import opencode_session_id

    preset = MODEL_PRESETS[provider]
    api_key = getattr(Settings.from_environment(), f"{provider}_api_key", None) or os.environ.get(preset["api_key_var"])

    models: list[str] = []
    try:
        import httpx

        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # OpenCode asks third-party coding agents to identify themselves.
        if provider != "commandcode":
            headers.update(gateway_headers(opencode_session_id()))

        response = httpx.get(f"{preset['base_url']}/models", headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()

        models = [
            entry["id"]
            for entry in payload.get("data", [])
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip()
            and (provider == "commandcode" or protocol(provider, entry["id"]) != "unsupported")
        ]
    except Exception:  # noqa: BLE001 — network/parse failure falls back, as Ollama does
        models = []

    # Fallback to preset models if the request returned nothing usable
    return sorted(set(models)) if models else preset["models"]


class ModelManager:
    """Manages model provider selection and switching."""

    def __init__(self):
        """Initialize model manager with current settings."""
        self.settings = Settings.from_environment()
        self.nova_config = NovaConfig()
        self.current_provider: ProviderType | None = None
        self.current_model: str | None = None

    def get_available_providers(self) -> list[tuple[str, dict[str, Any]]]:
        """Get list of providers that can actually be called right now.

        Readiness comes from
        :func:`novacode_cli.config.provider_auth.get_provider_auth_status`, which
        checks the credential store before the environment. Checking
        ``os.environ`` alone was wrong: a key saved through `/auth` lives in the
        OS keychain and only reaches the environment during startup hydration, so
        a keychain-only install reported every provider as unavailable.

        Returns:
            List of (provider_id, preset) tuples for available providers
        """
        from novacode_cli.config.provider_auth import get_all_auth_statuses

        statuses = get_all_auth_statuses(include_providers_without_keys=True)
        available = []
        for provider_id, preset in MODEL_PRESETS.items():
            status = statuses.get(provider_id)
            if status is not None and status.is_usable:
                available.append((provider_id, preset))
        return available

    def get_provider_statuses(self) -> dict[str, ProviderAuthStatus]:
        """Credential readiness for every provider, keyed by provider id.

        Built from one credential-store pass, so a caller rendering a screen
        does not probe the keychain once per provider.

        Returns:
            Mapping of provider id to its readiness status.
        """
        from novacode_cli.config.provider_auth import get_all_auth_statuses

        return get_all_auth_statuses(include_providers_without_keys=True)

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
        try:
            from novacode_cli.config.openai_chatgpt_auth import has_credentials

            if has_credentials():
                return ("OpenAI", os.environ.get("OPENAI_MODEL", "gpt-5-mini"))
        except Exception:  # noqa: BLE001 — status display is best-effort
            pass
        if self.settings.has_anthropic:
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
            return ("Anthropic", model)
        from novacode_cli.config.provider_auth import get_provider_auth_status
        if get_provider_auth_status("google").is_usable:
            model = os.environ.get("GOOGLE_MODEL", "gemini-3-pro-preview")
            return ("Google", model)
        if self.settings.has_openrouter:
            model = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
            return ("OpenRouter", model)
        if self.settings.has_opencode:
            model = os.environ.get("OPENCODE_MODEL", "glm-5.3")
            return ("OpenCode Go", model)
        if self.settings.has_opencode_zen:
            return ("OpenCode Zen", os.environ.get("OPENCODE_ZEN_MODEL", "big-pickle"))
        if self.settings.has_commandcode:
            model = os.environ.get("COMMANDCODE_MODEL", "deepseek/deepseek-v4-flash")
            return ("Command Code", model)
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
        from novacode_cli.config.credentials import CredentialStore

        preset = MODEL_PRESETS.get(provider)
        api_key_var = preset.get("api_key_var") if preset else None
        if not api_key_var:
            return None

        # Read through the credential store, not SecretManager directly: the
        # store is the one place that knows how a credential is filed, and a
        # second reader here is how the keychain and the TUI drifted apart.
        key = CredentialStore().get_key(api_key_var) or os.environ.get(api_key_var)
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

        # Check API key requirement. `resolve_api_key` is keychain-aware and
        # exports what it finds, so a key saved through `/auth` works here too —
        # an env-only check rejected it until the process was restarted.
        if preset["requires_api_key"] and not self.resolve_api_key(provider):
            if provider != "openai":
                api_key_var = preset["api_key_var"]
                raise ValueError(f"{preset['name']} requires {api_key_var} environment variable")
            from novacode_cli.config.model_create import _openai_auth_available

            if not _openai_auth_available():
                api_key_var = preset["api_key_var"]
                raise ValueError(
                    f"OpenAI requires {api_key_var} or a ChatGPT sign-in configured in /auth"
                )

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
