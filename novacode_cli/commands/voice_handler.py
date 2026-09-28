"""``/voice`` command — control local voice I/O (STT + VAD + TTS).

Subcommands::

    /voice                       show status (availability + current settings)
    /voice status
    /voice doctor                run full voice stack diagnostics
    /voice on | off              enable/disable voice (persisted default)
    /voice mode ptt|listen       push-to-talk vs always-listening
    /voice speak on|off          enable/disable spoken replies
    /voice test                  synthesize + play a phrase to verify the pipeline
    /voice download              pre-fetch the configured STT + TTS models
    /voice settings              show all voice settings with available providers
    /voice settings stt          choose STT provider (faster-whisper / deepgram)
    /voice settings tts          choose TTS provider (piper / elevenlabs / none)
    /voice settings stt deepgram  configure Deepgram API key + model
    /voice settings tts elevenlabs  configure ElevenLabs API key + voice ID

Live capture is driven from the TUI keybindings (ctrl+g push-to-talk,
ctrl+l toggle listening); this command manages the persisted preferences
and a quick end-to-end test. Persisted to ``~/.nova/Nova.config.json``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from novacode_cli import audio

if TYPE_CHECKING:
    from rich.console import Console

    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.states.Session import SessionState

_MODE_ALIASES = {"ptt": "push_to_talk", "push_to_talk": "push_to_talk", "listen": "listen"}
_ON_OFF = {"on": True, "off": False}
_MIN_MODE_TOKENS = 2
_SETTINGS_PROVIDER_TOKENS = 3


async def handle_voice_command(  # noqa: PLR0911 — command dispatcher, one return per subcommand
    cmd_args: str | None,
    session_state: SessionState,  # noqa: ARG001 — uniform command-handler signature
    console: Console,
) -> bool:
    """Dispatch a ``/voice`` subcommand. Returns ``True`` (command handled)."""
    from novacode_cli.config.nova_config import NovaConfig

    config = NovaConfig()
    try:
        tokens = shlex.split((cmd_args or "").strip())
    except ValueError as exc:
        console.print(f"[red]Could not parse arguments: {exc}[/red]")
        return True

    action = tokens[0].lower() if tokens else "status"

    if action == "status":
        _print_status(config, console)
        return True

    if action in ("on", "off"):
        cfg = config.set_voice_config(enabled=action == "on")
        state = "[green]on[/green]" if cfg["enabled"] else "[dim]off[/dim]"
        console.print(f"  Voice is now {state}.")
        if action == "on" and not audio.is_voice_available():
            console.print(f"  [yellow]{audio.install_hint()}[/yellow]")
        return True

    if action == "mode":
        if len(tokens) < _MIN_MODE_TOKENS or tokens[1].lower() not in _MODE_ALIASES:
            console.print("[yellow]Usage:[/yellow] /voice mode ptt|listen")
            return True
        mode = _MODE_ALIASES[tokens[1].lower()]
        config.set_voice_config(mode=mode)
        console.print(f"  [green]✓[/green] Voice mode set to [cyan]{mode}[/cyan].")
        return True

    if action == "speak":
        if len(tokens) < _MIN_MODE_TOKENS or tokens[1].lower() not in _ON_OFF:
            console.print("[yellow]Usage:[/yellow] /voice speak on|off")
            return True
        enabled = _ON_OFF[tokens[1].lower()]
        config.set_voice_config(speak_responses=enabled)
        state = "[green]on[/green]" if enabled else "[dim]off[/dim]"
        console.print(f"  Spoken responses are now {state}.")
        return True

    if action == "settings":
        if len(tokens) < _SETTINGS_PROVIDER_TOKENS:
            _print_settings(config, console)
            return True
        await _handle_settings(tokens[1:], config, console)
        return True

    if action == "test":
        await _run_test(config, console)
        return True

    if action == "download":
        await _run_download(config, console)
        return True

    if action == "doctor":
        _run_doctor(console)
        return True

    console.print(f"[yellow]Unknown /voice subcommand:[/yellow] {action}")
    return True


# ── Status ────────────────────────────────────────────────────────────────────


def _print_status(config: NovaConfig, console: Console) -> None:
    """Render availability + current voice settings."""
    cfg = config.get_voice_config()
    if audio.is_voice_available():
        console.print("  [green]●[/green] Local voice stack installed.")
    else:
        console.print(f"  [yellow]○ {audio.install_hint()}[/yellow]")
    console.print(
        f"  enabled=[cyan]{cfg['enabled']}[/cyan] mode=[cyan]{cfg['mode']}[/cyan] "
        f"speak=[cyan]{cfg['speak_responses']}[/cyan]"
    )
    console.print(
        f"  stt=[cyan]{cfg['stt_provider']}[/cyan] tts=[cyan]{cfg['tts_provider']}[/cyan]"
    )
    console.print("  [dim]In the TUI: ctrl+g = push-to-talk, ctrl+l = toggle listening.[/dim]")


# ── Settings ──────────────────────────────────────────────────────────────────


def _print_settings(config: NovaConfig, console: Console) -> None:
    """Show detailed settings with available providers."""
    cfg = config.get_voice_config()
    from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS

    console.print("[bold]Voice settings[/bold]")
    console.print(f"  enabled:      [cyan]{cfg['enabled']}[/cyan]")
    console.print(f"  mode:         [cyan]{cfg['mode']}[/cyan]")
    console.print(f"  speak:        [cyan]{cfg['speak_responses']}[/cyan]")
    console.print(f"  STT provider: [cyan]{cfg['stt_provider']}[/cyan]")
    _show_provider_options(STT_PROVIDERS, cfg["stt_provider"], config, console)
    console.print(f"  TTS provider: [cyan]{cfg['tts_provider']}[/cyan]")
    _show_provider_options(TTS_PROVIDERS, cfg["tts_provider"], config, console)


def _voice_provider_has_key(provider: str, config: NovaConfig) -> bool:
    """Whether a cloud voice provider has a usable key.

    Checks the credential store as well as the legacy config field: `/auth` and
    `/voice settings --key` write to the store, so a config-only check would
    report "(no key)" for a provider that is perfectly configured.

    Args:
        provider: Voice provider id (e.g. ``deepgram``).
        config: Config to read the legacy plaintext field from.

    Returns:
        True when a key is available from either source.
    """
    from novacode_cli.config.credentials import has_stored_credential
    from novacode_cli.config.provider_auth import credential_env_var

    pcfg = config.get_voice_provider_config(provider)
    if pcfg.get("api_key") or pcfg.get("key"):
        return True
    env_var = credential_env_var(provider)
    return bool(env_var) and has_stored_credential(env_var)


def _store_voice_key(provider: str, key: str, console: Console) -> bool:
    """Store a voice provider's key in the OS credential store.

    The key is written to the store, never to the config file, and is never
    echoed back to the console.

    Args:
        provider: Voice provider id (e.g. ``elevenlabs``).
        key: The API key to store.
        console: Console for reporting a failure.

    Returns:
        True when the key was stored.
    """
    from novacode_cli.config.credentials import set_credential
    from novacode_cli.config.provider_auth import credential_env_var

    env_var = credential_env_var(provider)
    if env_var is None:
        console.print(
            f"  [yellow]{provider} has no credential slot — "
            f"keeping the value in the config.[/yellow]"
        )
        return False
    outcome = set_credential(env_var, key)
    if not outcome.ok:
        detail = outcome.warnings[0] if outcome.warnings else "the credential store rejected it"
        console.print(f"  [red]Could not store the key:[/red] {detail}")
        return False
    console.print(
        f"  [green]✓[/green] Key stored for [cyan]{provider}[/cyan] (OS credential store)."
    )
    return True


def _route_key_flag(
    flags: dict[str, str],
    provider: str,
    config: NovaConfig,
    console: Console,
) -> None:
    """Send a ``--key`` flag to the credential store instead of the config file.

    Mutates *flags* in place: the key is removed so it is never written to
    ``Nova.config.json``, and ``api_key`` is explicitly blanked so a previously
    saved plaintext value does not linger next to the stored one.

    Args:
        flags: Parsed flag pairs (``api_key`` from ``--key``), modified in place.
        provider: Voice provider id being configured.
        config: Config holding the legacy plaintext field, if any.
        console: Console for reporting the outcome.
    """
    key = flags.pop("api_key", "") or flags.pop("key", "")
    if not key:
        return
    if _store_voice_key(provider, key, console):
        config.set_voice_provider_config(provider, api_key="", key="")
        return
    # No credential slot, or the store refused it: keep the old behaviour rather
    # than dropping the key the user just typed.
    flags["api_key"] = key


def _show_provider_options(
    providers: dict,
    current: str,
    config: NovaConfig,
    console: Console,
) -> None:
    """List available providers for STT or TTS, flagged with current + key status."""
    for key, meta in sorted(providers.items()):
        marker = "●" if key == current else "○"
        name = meta.get("name", key)
        desc = meta.get("description", "")
        if meta.get("requires_key"):
            key_ok = _voice_provider_has_key(key, config)
            key_status = " [green](key set)[/green]" if key_ok else " [yellow](no key)[/yellow]"
        else:
            key_status = ""
        console.print(f"    {marker} {name} — {desc}{key_status}")


async def _handle_settings(  # noqa: PLR0912 — settings sub-dispatch
    tokens: list[str],
    config: NovaConfig,
    console: Console,
) -> None:
    """Handle /voice settings <stt|tts> [provider] [--key x] [--model y] etc."""
    from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS

    action = tokens[0].lower()
    rest = tokens[1:] if len(tokens) > 1 else []

    if action == "stt":
        if not rest:
            console.print(
                f"  Current STT: [cyan]{config.get_voice_config()['stt_provider']}[/cyan]"
            )
            console.print("  Available: " + ", ".join(STT_PROVIDERS))
            return
        provider = rest[0].lower()
        if provider not in STT_PROVIDERS:
            console.print(f"  [red]Unknown STT provider:[/red] {provider}")
            return
        meta = STT_PROVIDERS[provider]
        if len(rest) > 1:
            # Configure — parse --key, --model
            flags = _parse_flags(rest[1:])
            _route_key_flag(flags, provider, config, console)
            config.set_voice_provider_config(provider, **flags)
            config.set_voice_config(stt_provider=provider)
            console.print(
                f"  [green]✓[/green] {meta['name']} configured and selected as active STT."
            )
        else:
            # Switch provider
            config.set_voice_config(stt_provider=provider)
            console.print(f"  [green]✓[/green] STT provider set to [cyan]{meta['name']}[/cyan].")
            if meta.get("requires_key") and not _voice_provider_has_key(provider, config):
                console.print(
                    f"  [yellow]Set key:[/yellow] /voice settings stt {provider} --key <key>"
                )
    elif action == "tts":
        if not rest:
            console.print(
                f"  Current TTS: [cyan]{config.get_voice_config()['tts_provider']}[/cyan]"
            )
            console.print("  Available: " + ", ".join(TTS_PROVIDERS))
            return
        provider = rest[0].lower()
        if provider not in TTS_PROVIDERS:
            console.print(f"  [red]Unknown TTS provider:[/red] {provider}")
            return
        meta = TTS_PROVIDERS[provider]
        if len(rest) > 1:
            flags = _parse_flags(rest[1:])
            _route_key_flag(flags, provider, config, console)
            config.set_voice_provider_config(provider, **flags)
            config.set_voice_config(tts_provider=provider)
            console.print(
                f"  [green]✓[/green] {meta['name']} configured and selected as active TTS."
            )
        else:
            config.set_voice_config(tts_provider=provider)
            console.print(f"  [green]✓[/green] TTS provider set to [cyan]{meta['name']}[/cyan].")
            if meta.get("requires_key") and not _voice_provider_has_key(provider, config):
                console.print(
                    f"  [yellow]Set key:[/yellow] /voice settings tts {provider} --key <key>"
                )
        # Orpheus is an optional heavy dep — warn if selected but not installed.
        if provider == "orpheus" and not audio.is_orpheus_available():
            console.print(f"  [yellow]{audio.orpheus_install_hint()}[/yellow]")
        if provider == "pocket" and not audio.is_pocket_available():
            console.print(f"  [yellow]{audio.pocket_install_hint()}[/yellow]")
    else:
        console.print(f"[yellow]Unknown settings category:[/yellow] {action}. Use stt or tts.")


# ── Test ─────────────────────────────────────────────────────────────────────


async def _run_test(config: NovaConfig, console: Console) -> None:
    """Speak a test phrase to verify synthesis + playback end-to-end."""
    if not audio.is_voice_available():
        console.print(f"  [yellow]{audio.install_hint()}[/yellow]")
        return
    from novacode_cli.audio.pipeline import VoicePipeline

    cfg = config.get_voice_config()
    console.print("  [dim]Synthesizing test phrase…[/dim]")
    try:
        pipeline = VoicePipeline(
            stt_provider=cfg.get("stt_provider", "faster-whisper"),
            tts_provider=cfg.get("tts_provider", "piper"),
            provider_configs=cfg.get("providers", {}),
        )
        await pipeline.speak("Nova voice output is working.")
        console.print("  [green]✓[/green] Heard it? Voice output is working.")
    except Exception as exc:  # noqa: BLE001 — surface any audio/device error to the user
        console.print(f"  [red]Voice test failed:[/red] {exc}")


# ── Download ─────────────────────────────────────────────────────────────────


async def _run_download(config: NovaConfig, console: Console) -> None:
    """Pre-fetch the configured STT + TTS models so first use isn't laggy.

    Downloads on demand via the pipeline's ``warmup`` (off the event loop). Safe
    to re-run — already-cached models are detected and skipped.
    """
    if not audio.is_voice_available():
        console.print(f"  [yellow]{audio.install_hint()}[/yellow]")
        return
    from novacode_cli.audio.pipeline import VoicePipeline

    cfg = config.get_voice_config()
    pipeline = VoicePipeline(
        stt_provider=cfg.get("stt_provider", "faster-whisper"),
        tts_provider=cfg.get("tts_provider", "piper"),
        provider_configs=cfg.get("providers", {}),
    )
    try:
        pending = pipeline.downloads_pending()
    except Exception as exc:  # noqa: BLE001 — detection is best-effort
        console.print(f"  [yellow]Could not check model cache: {exc}[/yellow]")
        pending = ["STT", "TTS"]  # download anyway

    if not pending:
        console.print("  [green]✓[/green] Voice models are already downloaded.")
        return

    console.print(
        f"  [dim]Downloading voice models ({', '.join(pending)})… this may take a while.[/dim]"
    )
    try:
        await pipeline.warmup()
        console.print("  [green]✓[/green] Voice models ready.")
    except Exception as exc:  # noqa: BLE001 — surface any download/network error
        console.print(f"  [red]Download failed:[/red] {exc}")


# ── Doctor ────────────────────────────────────────────────────────────────────


def _run_doctor(console: Console) -> None:
    """Run full voice stack diagnostics and print them."""
    console.print("  [bold]Voice diagnostics[/bold]")
    console.print()
    lines = audio.diagnose_voice()
    for line in lines:
        console.print(f"  {line}")
    console.print()
    # Summarize.
    from novacode_cli.audio import is_voice_available

    if is_voice_available():
        console.print("  [bold green]Voice stack is complete.[/bold green]")
    else:
        console.print(f"  [bold yellow]{audio.install_hint()}[/bold yellow]")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_flags(tokens: list[str]) -> dict[str, str]:
    """Parse ``--key value`` flag pairs from a token list."""
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and i + 1 < len(tokens):
            key = tok[2:]
            if key == "key":
                key = "api_key"
            flags[key] = tokens[i + 1]
            i += 2
        else:
            i += 1
    return flags
