"""First-run onboarding wizard and secret management for Nova CLI.

This module provides secure storage of API keys and interactive onboarding
workflow for first-time setup.
"""

import json
import logging
import os
import stat
import sys
import time
from contextlib import contextmanager
from getpass import getpass
from typing import Any

import requests
from rich.console import Console
from rich.panel import Panel
from pathlib import Path
from novacode_cli.config.config import HOME_DIR
from novacode_cli.config.nova_config import NovaConfig

if sys.platform == "win32":
    import io

    console = Console(file=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
else:
    console = Console()

logger = logging.getLogger(__name__)

# API key names for all supported providers
API_KEY_NAMES = {
    "tavily": "tavily_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
    "openrouter": "openrouter_api_key",
    "opencode": "opencode_api_key",
    "nvidia": "nvidia_api_key",
    "groq": "groq_api_key",
    # Cloud voice providers. Listed here, not just in the audio registry, so the
    # startup hydration pass exports a keychain-stored voice key too — the STT and
    # TTS backends read the environment.
    "deepgram": "deepgram_api_key",
    "elevenlabs": "elevenlabs_api_key",
    "jev": "typesafe_api_key",
    "systemone": "system_one_api_key",
}


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON data atomically to *path* via a temp file + replace.

    Uses Path.replace (os.replace), not rename: on Windows rename raises
    FileExistsError when the target exists; replace overwrites atomically.
    """
    from novacode_cli.security.secret_files import write_secret_json
    write_secret_json(path, data)


def _acquire_lock(lock_path: Path, timeout: float = 5.0) -> bool:
    """Try to acquire an exclusive lock file with a timeout.

    Returns True if the lock was acquired, False if it timed out.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            # Check if the lock is stale (>2 seconds old)
            try:
                mtime = lock_path.stat().st_mtime
                if time.time() - mtime > 2.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    return False


def _release_lock(lock_path: Path) -> None:
    """Release an exclusive lock file."""
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def _temporary_env(key: str, value: str):
    """Temporarily set an environment variable, restoring the original on exit."""
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def load_secrets_into_env() -> None:
    """Copy stored API keys from the secret manager into ``os.environ``.

    Model clients (ChatOpenAI, ChatAnthropic, ...) read their keys from
    environment variables, but keys may be stored only in the OS keychain.
    This hydrates them into the environment — without overriding values already
    set via ``.env`` or the shell — so providers work at startup and after
    switching via ``/model``. Env var name is derived as ``<KEY>.upper()``
    (e.g. ``openrouter_api_key`` -> ``OPENROUTER_API_KEY``).
    """
    try:
        secret_manager = SecretManager()
    except Exception:  # noqa: BLE001
        return  # keyring unavailable — nothing to hydrate

    for key_name in API_KEY_NAMES.values():
        try:
            value = secret_manager.get_secret(key_name)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            os.environ.setdefault(key_name.upper(), value)


class SecretManager:
    """Manages secure storage and retrieval of API keys.

    Uses OS keychain (via keyring library) as primary storage,
    with fallback to permission-restricted JSON file.
    """

    SERVICE_NAME = "Nova-cli"
    FALLBACK_FILE = HOME_DIR / "secrets.json"

    def __init__(self) -> None:
        """Initialize secret manager with keyring or file fallback."""
        self.use_keyring = False
        try:
            import keyring

            self.keyring = keyring
            # Test if keyring is actually available (not null backend)
            backend = keyring.get_keyring()
            # Check by module path: type(backend).__name__ returns "Keyring" (not "fail.Keyring"),
            # so we must check the full module-qualified name
            backend_fq_name = f"{type(backend).__module__}.{type(backend).__name__}"
            if backend_fq_name != "keyring.backends.fail.Keyring":
                self.use_keyring = True
        except (ImportError, RuntimeError):
            pass

        if not self.use_keyring:
            console.print(
                "[yellow]⚠ OS keychain not available, using file-based storage[/yellow]"
            )
            self._ensure_fallback_file()

    def _ensure_fallback_file(self) -> None:
        """Create secrets.json with secure permissions if it doesn't exist."""
        if not self.FALLBACK_FILE.exists():
            self.FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "umask"):
                # Set restrictive umask before creation to prevent brief
                # window where file is world-readable (TOCTOU fix)
                old_umask = os.umask(0o077)
                try:
                    self.FALLBACK_FILE.write_text("{}", encoding="utf-8")
                finally:
                    os.umask(old_umask)
            else:
                self.FALLBACK_FILE.write_text("{}", encoding="utf-8")
            # Ensure restrictive permissions even if umask was bypassed
            if hasattr(os, "chmod"):
                os.chmod(self.FALLBACK_FILE, stat.S_IRUSR | stat.S_IWUSR)

    def store_secret(self, key: str, value: str) -> bool:
        """Store a secret (API key) securely.

        Args:
            key: The secret key name (e.g., "tavily_api_key")
            value: The secret value

        Returns:
            True if storage was successful, False otherwise
        """
        try:
            if self.use_keyring:
                self.keyring.set_password(self.SERVICE_NAME, key, value)
                return True

            # Fallback: JSON file with locking + atomic write
            lock_path = self.FALLBACK_FILE.with_suffix(".lock")
            if not _acquire_lock(lock_path):
                logger.warning("Could not acquire lock for secrets file (store)")
                console.print("[red]✗ Could not lock secrets file[/red]")
                return False
            try:
                secrets = {}
                if self.FALLBACK_FILE.exists():
                    secrets = json.loads(self.FALLBACK_FILE.read_text(encoding="utf-8"))
                secrets[key] = value
                _atomic_write_json(self.FALLBACK_FILE, secrets)
            finally:
                _release_lock(lock_path)
            return True
        except OSError as e:
            logger.error("Failed to store secret %s: %s", key, e, exc_info=True)
            console.print(f"[red]✗ Failed to store secret: {e}[/red]")
            return False
        except json.JSONDecodeError as e:
            logger.error(
                "Corrupted secrets file when storing %s: %s", key, e, exc_info=True
            )
            console.print("[red]✗ Corrupted secrets file[/red]")
            return False

    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret (API key).

        Args:
            key: The secret key name (e.g., "tavily_api_key")

        Returns:
            The secret value, or None if not found
        """
        try:
            if self.use_keyring:
                return self.keyring.get_password(self.SERVICE_NAME, key)

            # Fallback: JSON file with lock
            if not self.FALLBACK_FILE.exists():
                return None

            lock_path = self.FALLBACK_FILE.with_suffix(".lock")
            _acquire_lock(lock_path, timeout=2.0)
            try:
                secrets = json.loads(self.FALLBACK_FILE.read_text(encoding="utf-8"))
                return secrets.get(key)
            finally:
                _release_lock(lock_path)
        except OSError as e:
            logger.warning("Failed to read secrets file for %s: %s", key, e)
            return None
        except json.JSONDecodeError as e:
            logger.warning("Corrupted secrets file when reading %s: %s", key, e)
            return None
        except Exception as e:
            logger.warning("Unexpected error reading secret %s: %s", key, e)
            return None

    def delete_secret(self, key: str) -> bool:
        """Delete a secret (API key).

        Args:
            key: The secret key name (e.g., "tavily_api_key")

        Returns:
            True if deletion was successful, False otherwise
        """
        try:
            if self.use_keyring:
                try:
                    self.keyring.delete_password(self.SERVICE_NAME, key)
                except self.keyring.errors.PasswordDeleteError:
                    pass
                return True

            # Fallback: JSON file with locking + atomic write
            if not self.FALLBACK_FILE.exists():
                return True

            lock_path = self.FALLBACK_FILE.with_suffix(".lock")
            if not _acquire_lock(lock_path):
                logger.warning("Could not acquire lock for secrets file (delete)")
                return False
            try:
                secrets = json.loads(self.FALLBACK_FILE.read_text(encoding="utf-8"))
                secrets.pop(key, None)
                _atomic_write_json(self.FALLBACK_FILE, secrets)
            finally:
                _release_lock(lock_path)
            return True
        except OSError as e:
            logger.error("Failed to delete secret %s: %s", key, e, exc_info=True)
            return False

    def list_secrets(self) -> list[str]:
        """List all stored secret keys.

        Returns:
            List of secret key names (not values)
        """
        if self.use_keyring:
            # Keyring doesn't provide list functionality, so we check known keys
            secrets: list[str] = []
            for key in API_KEY_NAMES.values():
                try:
                    if self.keyring.get_password(self.SERVICE_NAME, key):
                        secrets.append(key)
                except Exception:  # noqa: BLE001
                    continue
            return secrets
        # Fallback: JSON file
        if self.FALLBACK_FILE.exists():
            try:
                lock_path = self.FALLBACK_FILE.with_suffix(".lock")
                _acquire_lock(lock_path, timeout=2.0)
                try:
                    secrets = json.loads(self.FALLBACK_FILE.read_text(encoding="utf-8"))
                    return list(secrets.keys())
                finally:
                    _release_lock(lock_path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to list secrets: %s", e)
                return []
        return []


class OnboardingWizard:
    """Interactive wizard for first-time setup of Nova CLI.

    Guides users through:
    1. LLM provider selection
    2. Provider-specific configuration
    3. Tavily API key setup
    4. Connection testing
    5. Configuration saving
    """

    PROVIDERS = {
        "1": {"name": "ollama", "display": "Ollama (local)"},
        "2": {"name": "openai", "display": "OpenAI"},
        "3": {"name": "anthropic", "display": "Anthropic"},
        "4": {"name": "google", "display": "Google (Gemini)"},
        "5": {"name": "openrouter", "display": "OpenRouter"},
        "6": {"name": "opencode", "display": "OpenCode Go"},
        "7": {"name": "nvidia", "display": "NVIDIA NIM"},
    }

    def __init__(self) -> None:
        """Initialize the onboarding wizard."""
        self.secret_manager = SecretManager()
        self.config_path = HOME_DIR / "config.json"
        self.Nova_config = NovaConfig()

    def run(self) -> bool:
        """Run the interactive onboarding wizard.

        Returns:
            True if onboarding completed successfully, False otherwise
        """
        console.print()
        console.print(
            Panel.fit(
                "[bold cyan]Welcome to Nova 👋[/bold cyan]\n\n"
                "Let's set up your AI coding assistant.",
                border_style="cyan",
            )
        )
        console.print()

        # Step 1: Choose LLM provider
        provider = self._prompt_provider()
        if not provider:
            return False

        # Step 2: Configure provider
        provider_config = self._prompt_provider_config(provider)
        if not provider_config:
            return False

        # Step 3: Get Tavily API key (optional - can skip for now)
        tavily_key = self._prompt_tavily_key()

        # Step 4: Test connections
        console.print()
        console.print("[bold]Testing connections:[/bold]")
        if not self._test_connections(provider, provider_config, tavily_key):
            console.print()
            console.print(
                "[yellow]⚠ Connection tests failed. "
                "You can continue but may need to fix configuration later.[/yellow]"
            )
            response = input("Continue anyway? [y/N]: ").strip().lower()
            if response != "y":
                return False

        # Step 5: Save configuration
        self._save_config(provider, provider_config, tavily_key)

        console.print()
        console.print("[green]✓ Setup complete![/green]")
        console.print()
        console.print(f"[dim]Configuration saved to {self.config_path}[/dim]")
        if self.secret_manager.use_keyring:
            console.print("[dim]API keys stored in system keychain[/dim]")
        else:
            console.print(
                f"[dim]API keys stored in {self.secret_manager.FALLBACK_FILE}[/dim]"
            )
        console.print()
        console.print("[bold cyan]You're ready to go![/bold cyan]\"")

        return True

    def _prompt_provider(self) -> str | None:
        """Prompt user to select an LLM provider.

        Returns:
            Provider name (ollama/openai/anthropic/groq) or None if cancelled
        """
        console.print("[bold]Choose LLM provider:[/bold]")
        for key, provider in self.PROVIDERS.items():
            console.print(f"  {key}. {provider['display']}")
        console.print()

        choice = input("> ").strip()

        if choice not in self.PROVIDERS:
            console.print("[red]✗ Invalid choice[/red]")
            return None

        return self.PROVIDERS[choice]["name"]

    def _prompt_provider_config(self, provider: str) -> dict[str, Any] | None:
        """Prompt for provider-specific configuration.

        Args:
            provider: Provider name (ollama/openai/anthropic)

        Returns:
            Configuration dict or None if cancelled
        """
        console.print()
        console.print(f"[bold]{provider.title()} configuration:[/bold]")

        if provider == "ollama":
            # Ollama: just needs host
            host = input("  Host [http://localhost:11434]: ").strip()
            if not host:
                host = "http://localhost:11434"
            return {"host": host}

        # Cloud providers: need API key
        api_key = getpass(f"  {provider.title()} API key: ")
        if not api_key:
            console.print(f"[red]✗ {provider.title()} API key required[/red]")
            return None

        # Store API key in secret manager
        key_name = API_KEY_NAMES[provider]
        self.secret_manager.store_secret(key_name, api_key)

        return {"api_key": api_key}

    def _prompt_tavily_key(self) -> str | None:
        """Prompt for Tavily Search API key (optional).

        Returns:
            Tavily API key or None if skipped
        """
        console.print()
        console.print("[bold]Search provider (Tavily):[/bold]")
        console.print("  [dim]Required for web search. Press Enter to skip.[/dim]")
        tavily_key = getpass("  Tavily API key: ")

        if tavily_key:
            self.secret_manager.store_secret(API_KEY_NAMES["tavily"], tavily_key)
            return tavily_key

        return None

    def _test_connections(
        self,
        provider: str,
        provider_config: dict[str, Any],
        tavily_key: str | None,
    ) -> bool:
        """Test connections to the LLM provider and Tavily.

        Args:
            provider: Provider name
            provider_config: Provider configuration
            tavily_key: Tavily API key (optional)

        Returns:
            True if all tests passed, False otherwise
        """
        all_passed = True

        # Test LLM provider
        if provider == "ollama":
            console.print(f"  → Testing {provider} connection... ", end="")
            try:
                host = provider_config["host"]
                response = requests.get(f"{host}/api/tags", timeout=5)
                if response.status_code == 200:  # noqa: PLR2004
                    console.print("[green]✓[/green]")
                else:
                    console.print(f"[red]✗ (HTTP {response.status_code})[/red]")
                    all_passed = False
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]✗ ({e})[/red]")
                all_passed = False
        else:
            # For cloud providers, try to create model instance
            console.print(f"  → Testing {provider} connection... ", end="")
            try:
                api_key = provider_config["api_key"]
                env_key = f"{provider.upper()}_API_KEY"
                with _temporary_env(env_key, api_key):
                    # Try to create model using ModelManager
                    from novacode_cli.config.model_manager import ModelManager

                    model_manager = ModelManager()
                    _ = model_manager.create_model_for_provider(provider)

                console.print("[green]✓[/green]")
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]✗ ({e})[/red]")
                all_passed = False

        # Test Tavily if key provided
        if tavily_key:
            console.print("  → Testing Tavily connection... ", end="")
            try:
                from tavily import TavilyClient

                client = TavilyClient(api_key=tavily_key)
                # Simple test query
                _ = client.search("test", max_results=1)
                console.print("[green]✓[/green]")
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]✗ ({e})[/red]")
                all_passed = False

        return all_passed

    def _save_config(
        self,
        provider: str,
        provider_config: dict[str, Any],
        tavily_key: str | None,
        model_name: str | None = None,
    ) -> None:
        """Save configuration to config.json and secrets.

        Args:
            provider: Provider name
            provider_config: Provider configuration
            tavily_key: Tavily API key (optional)
            model_name: The model to start on. When given it is used as it is
                and nothing is printed: the setup screen passes the model it
                found, and a ``console.print`` from here would land in the
                middle of a running Textual screen.
        """
        # Build config (non-secret parts only)
        config: dict[str, Any] = {
            "provider": provider,
            "onboarding_completed": True,
        }

        if provider == "ollama":
            config["ollama"] = {"host": provider_config["host"]}
        # For cloud providers, API key is already stored in secret manager

        if tavily_key:
            config["search"] = {"provider": "tavily"}

        # Save to config.json
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        # Also save to NovaConfig for backward compatibility
        if model_name:
            pass
        elif provider == "ollama":
            # Check if Ollama models are installed
            from novacode_cli.config.model_manager import get_ollama_models

            available_models = get_ollama_models()

            if "minimax-m2.1:cloud" in available_models:
                model_name = "minimax-m2.1:cloud"
            elif available_models:
                # Use the first available model
                model_name = available_models[0]
                console.print()
                console.print(
                    f"[yellow]⚠ minimax-m2.1:cloud not found, using {model_name}[/yellow]"
                )
                console.print()
            else:
                # No models installed
                model_name = "minimax-m2.1:cloud"  # Set as default anyway
                console.print()
                console.print(
                    "[yellow]⚠ No Ollama models found on your system[/yellow]"
                )
                console.print()
                console.print("[bold]To install Ollama models:[/bold]")
                console.print(
                    "  1. Install a model: [cyan]ollama pull minimax-m2.1:cloud[/cyan]"
                )
                console.print(
                    "  2. Or browse models: [cyan]https://ollama.com/library[/cyan]"
                )
                console.print()
                console.print(
                    "[dim]After installing models, use the [bold]/model[/bold] command to configure them[/dim]"
                )
                console.print()
        else:
            # Cloud providers: persist the provider's real default model id
            # (e.g. "anthropic/claude-3.5-sonnet") so model creation gets a valid
            # name rather than the literal "default".
            from novacode_cli.config.model_manager import MODEL_PRESETS

            preset = MODEL_PRESETS.get(provider)
            model_name = preset["default_model"] if preset else "default"

        self.Nova_config.set_model_config(provider, model_name)

        # Create completion marker
        (HOME_DIR / ".onboarded").touch()
