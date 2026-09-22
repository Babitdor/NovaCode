"""Utility functions for getting model information."""

import os
import subprocess
import json
from typing import Tuple, List


def get_ollama_models() -> List[str]:
    """Get list of available Ollama models.
    
    Returns:
        List of model names available in Ollama
    """
    try:
        result = subprocess.run(
            ["ollama", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            # Parse JSON output
            data = json.loads(result.stdout)
            models = []
            for model in data.get("models", []):
                models.append(model["name"])
            return models
        else:
            # Fallback: try parsing plain text output
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                models = []
                lines = result.stdout.strip().split('\n')
                # Skip header line
                for line in lines[1:]:
                    if line.strip():
                        # Model name is first column
                        parts = line.split()
                        if parts:
                            models.append(parts[0])
                return models
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    
    return []


def get_model_info() -> Tuple[str, str, str]:
    """Get current model provider, model name, and display name.
    
    Returns:
        Tuple of (provider, model_name, display_name)
        Example: ("anthropic", "claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet")
    """
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.config.config import settings
    
    # Load saved configuration
    nova_config = NovaConfig()
    saved_model_config = nova_config.get_model_config()
    
    if saved_model_config:
        provider = saved_model_config["provider"]
        model_name = saved_model_config["model"]
        display_name = _get_display_name(provider, model_name)
        return provider, model_name, display_name
    
    # Fall back to environment variables
    if settings.has_openai:
        provider = "openai"
        model_name = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
        display_name = _get_display_name(provider, model_name)
        return provider, model_name, display_name
    
    if settings.has_anthropic:
        provider = "anthropic"
        model_name = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
        display_name = _get_display_name(provider, model_name)
        return provider, model_name, display_name
    
    if settings.has_google:
        provider = "google"
        model_name = os.environ.get("GOOGLE_MODEL", "gemini-3-pro-preview")
        display_name = _get_display_name(provider, model_name)
        return provider, model_name, display_name
    
    # Default to Ollama
    provider = "ollama"
    
    # Try to get model from environment
    model_name = os.environ.get("OLLAMA_MODEL")
    
    # If no model in environment, try to get first available from ollama list
    if not model_name:
        available_models = get_ollama_models()
        if available_models:
            model_name = available_models[0]
        else:
            model_name = "deepseek-v4.1-flash:cloud"  # Fallback default
    
    display_name = _get_display_name(provider, model_name)
    return provider, model_name, display_name


def get_current_provider() -> str | None:
    """Provider of the model currently in effect, or None if undeterminable.

    Used when saving a session so a later resume can rebuild the exact model
    rather than falling back to whatever the global config says at that time.
    Reads the same precedence chain as :func:`get_model_info` (saved config,
    then env keys) so the recorded provider always matches the live model.
    """
    try:
        provider, _model_name, _display = get_model_info()
    except Exception:  # noqa: BLE001 — a save must never fail on this
        return None
    return provider or None


def _get_display_name(provider: str, model_name: str) -> str:
    """Get a human-readable display name for the model.
    
    Args:
        provider: The provider name (openai, anthropic, google, ollama)
        model_name: The model identifier
        
    Returns:
        Human-readable display name
    """
    # Common model name mappings
    model_display_names = {
        # Anthropic
        "claude-sonnet-4-5-20250929": "Claude 4.5 Sonnet",
        "claude-opus-4-5-20251001": "Claude 4.5 Opus",
        "claude-3-5-sonnet-20241022": "Claude 3.5 Sonnet",
        "claude-3-5-sonnet-20240620": "Claude 3.5 Sonnet",
        "claude-3-5-haiku-20241022": "Claude 3.5 Haiku",
        "claude-3-opus-20240229": "Claude 3 Opus",
        "claude-3-sonnet-20240229": "Claude 3 Sonnet",
        "claude-3-haiku-20240307": "Claude 3 Haiku",
        
        # OpenAI
        "gpt-5-mini": "GPT-5 Mini",
        "gpt-4o": "GPT-4o",
        "gpt-4o-mini": "GPT-4o Mini",
        "gpt-4-turbo": "GPT-4 Turbo",
        "gpt-4": "GPT-4",
        
        # Google
        "gemini-3-pro-preview": "Gemini 3 Pro",
        "gemini-2.0-flash-exp": "Gemini 2.0 Flash",
        "gemini-1.5-pro": "Gemini 1.5 Pro",
        "gemini-1.5-flash": "Gemini 1.5 Flash",
        
        # Ollama (common models)
        "qwen3-coder:480b-cloud": "Qwen3 Coder 480B",
        "qwen3-vl:235b-cloud": "Qwen3 VL 235B",
        "llama3.1:8b": "Llama 3.1 8B",
        "llama3.1:70b": "Llama 3.1 70B",
        "codellama:34b": "Code Llama 34B",
    }
    
    # Check if we have a display name
    if model_name in model_display_names:
        return model_display_names[model_name]
    
    # Try to create a reasonable display name from the model name
    # Remove common prefixes and suffixes
    display = model_name
    
    # Remove version suffixes like :480b-cloud
    if ":" in display:
        display = display.split(":")[0]
    
    # Capitalize and format
    display = display.replace("-", " ").replace("_", " ")
    display = " ".join(word.capitalize() for word in display.split())
    
    return display


def get_provider_icon(provider: str) -> str:
    """Get an icon for the provider.
    
    Args:
        provider: The provider name
        
    Returns:
        Icon string
    """
    icons = {
        "openai": "🤖",
        "anthropic": "🧠",
        "google": "🌟",
        "ollama": "🦙",
    }
    return icons.get(provider, "🔧")


def get_provider_color(provider: str) -> str:
    """Get a color for the provider.
    
    Args:
        provider: The provider name
        
    Returns:
        Color string for Rich
    """
    colors = {
        "openai": "green",
        "anthropic": "blue",
        "google": "red",
        "ollama": "yellow",
    }
    return colors.get(provider, "white")