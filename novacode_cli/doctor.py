"""Setup validation command for Nova CLI.

Validates configuration, API keys, and connections to services.
"""

import json

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from novacode_cli.config.config import HOME_DIR, Settings
from novacode_cli.config.model_create import create_model
from novacode_cli.onboarding import SecretManager

import sys
if sys.platform == "win32":
    import io
    console = Console(file=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
else:
    console = Console()


def run_doctor() -> int:
    """Run comprehensive setup validation.

    Returns:
        Exit code: 0 if all checks passed, 1 if any failures
    """
    console.print()
    console.print(Panel.fit("[bold]Nova Setup Validation[/bold]", border_style="cyan"))
    console.print()

    all_passed = True
    results = []

    # Check 1: Configuration file exists
    config_file = HOME_DIR / "config.json"
    if config_file.exists():
        results.append(("✓", "Configuration file found", config_file))
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
            provider = config.get("provider", "Not set")
            results.append(("✓", f"LLM provider configured ({provider})", ""))
        except Exception as e:  # noqa: BLE001
            results.append(("✗", f"Config file invalid: {e}", ""))
            all_passed = False
    else:
        results.append(("✗", "Configuration file not found", config_file))
        results.append(
            ("ℹ", "Run 'Nova init' to set up configuration", ""),
        )
        all_passed = False

    # Check 2: Secrets/API keys
    secret_manager = SecretManager()
    secrets = secret_manager.list_secrets()
    if secrets:
        for secret_key in secrets:
            # Mask the key name for display
            display_key = secret_key.replace("_api_key", "").title()
            results.append(("✓", f"{display_key} API key set", ""))
    else:
        results.append(("⚠", "No API keys found", ""))
        results.append(("ℹ", "Run 'Nova secrets set <provider>_api_key' to add keys", ""))

    # Check 3: LLM provider connection
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
            provider = config.get("provider")

            if provider == "ollama":
                # Test Ollama connection
                ollama_host = config.get("ollama", {}).get("host", "http://localhost:11434")
                try:
                    response = requests.get(f"{ollama_host}/api/tags", timeout=5)
                    if response.status_code == 200:  # noqa: PLR2004
                        results.append(("✓", "Ollama connection successful", ollama_host))

                        # Runtime check: actual allocated context + GPU/CPU split
                        # for the currently-loaded model (via `ollama ps`).
                        import os as _os

                        from novacode_cli.context._dynamic import (
                            check_ollama_offloading,
                            get_ollama_context_length,
                            get_ollama_num_ctx,
                            get_ollama_runtime_info,
                            is_ollama_cloud_model,
                        )

                        _model = config.get("model") or _os.environ.get(
                            "OLLAMA_MODEL", "deepseek-v4.1-flash:cloud"
                        )
                        if is_ollama_cloud_model(_model):
                            # Cloud model: runs on Ollama's servers, not local
                            # VRAM. `ollama ps` won't list it; `ollama show`
                            # reports its (maximum) context length.
                            _max = get_ollama_context_length(_model)
                            _detail = f"{_max:,} tokens (max)" if _max else "via ollama show"
                            results.append(
                                ("ℹ", f"Cloud model '{_model}' — runs remotely", _detail)
                            )
                            _rt = None
                        else:
                            _rt = get_ollama_runtime_info(_model)
                        if _rt is None and not is_ollama_cloud_model(_model):
                            results.append(
                                (
                                    "ℹ",
                                    f"Ollama model '{_model}' not loaded yet "
                                    f"(requesting num_ctx={get_ollama_num_ctx():,})",
                                    "loads on first message",
                                )
                            )
                        elif _rt is not None:
                            ctx = _rt.get("context")
                            if ctx:
                                results.append(
                                    (
                                        "✓",
                                        f"Allocated context: {ctx:,} tokens",
                                        f"requested {get_ollama_num_ctx():,}",
                                    )
                                )
                            _offload = check_ollama_offloading(_model)
                            if _offload:
                                results.append(("⚠", _offload, ""))
                            elif _rt.get("processor"):
                                results.append(
                                    ("✓", f"Model on GPU ({_rt['processor']})", "")
                                )
                    else:
                        results.append(
                            (
                                "✗",
                                f"Ollama connection failed (HTTP {response.status_code})",
                                ollama_host,
                            )
                        )
                        all_passed = False
                except Exception as e:  # noqa: BLE001
                    results.append(("✗", f"Ollama connection failed: {e}", ollama_host))
                    all_passed = False
            else:
                # Test cloud provider by creating model instance
                try:
                    settings = Settings.from_environment()
                    _ = create_model()
                    results.append(("✓", f"{provider.title()} connection successful", ""))
                except Exception as e:  # noqa: BLE001
                    results.append(("✗", f"{provider.title()} connection failed: {e}", ""))
                    all_passed = False

        except Exception as e:  # noqa: BLE001
            results.append(("✗", f"Provider check failed: {e}", ""))
            all_passed = False

    # Check 4: Tavily connection (if configured)
    tavily_key = secret_manager.get_secret("tavily_api_key")
    if tavily_key:
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=tavily_key)
            _ = client.search("test", max_results=1)
            results.append(("✓", "Tavily connection successful", ""))
        except Exception as e:  # noqa: BLE001
            results.append(("✗", f"Tavily connection failed: {e}", ""))
            all_passed = False

    # Check 5: File permissions on secrets.json (if using fallback)
    secrets_file = SecretManager.FALLBACK_FILE
    if secrets_file.exists() and not secret_manager.use_keyring:
        try:
            import stat as stat_module

            file_stat = secrets_file.stat()
            mode = file_stat.st_mode
            # Check if file is readable by others (should NOT be)
            if mode & (stat_module.S_IRGRP | stat_module.S_IROTH):
                results.append(
                    (
                        "⚠",
                        "Secrets file has insecure permissions",
                        f"Run: chmod 600 {secrets_file}",
                    )
                )
                all_passed = False
            else:
                results.append(("✓", "Secrets file permissions secure", ""))
        except Exception:  # noqa: BLE001, S110
            # Skip permission check on Windows or if stat fails
            pass

    # Check 6: Voice stack
    from novacode_cli import audio as _audio

    if _audio.is_voice_available():
        results.append(("✓", "Voice stack imported", ""))
        # Deeper: try sounddevice early
        try:
            import sounddevice as _sd
            _sd.check_input_settings(device=None)
            dll_path = getattr(_sd, "_libname", "?")
            results.append(("✓", "PortAudio / default input device", dll_path))
        except Exception as _v_e:
            results.append(("✗", f"PortAudio: {_v_e}", f"See /voice doctor"))
            all_passed = False
    else:
        missing = ", ".join(_audio.missing_deps())
        results.append(("⚠", "Voice stack incomplete", missing))
        results.append(("ℹ", "See /voice doctor or run:", _audio.install_hint()))

    # Display results in a table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Status", style="bold", width=3)
    table.add_column("Check")
    table.add_column("Details", style="dim")

    for status, check, details in results:
        status_style = {
            "✓": "green",
            "✗": "red",
            "⚠": "yellow",
            "ℹ": "blue",
        }.get(status, "white")

        table.add_row(
            f"[{status_style}]{status}[/{status_style}]",
            check,
            str(details) if details else "",
        )

    console.print(table)
    console.print()

    if all_passed:
        console.print("[bold green]Everything looks good! 🎉[/bold green]")
    else:
        console.print(
            "[bold yellow]Some checks failed. Please review the issues above.[/bold yellow]"
        )

    console.print()
    return 0 if all_passed else 1
