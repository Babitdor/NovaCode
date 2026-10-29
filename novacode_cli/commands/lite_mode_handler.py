"""Shared Lite Mode controls; compression engines and saved keys remain compatible."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.config.nova_config import CAVEMAN_LEVELS, NovaConfig

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry

USAGE = (
    "Usage: /lite-mode [settings|on|off|status|caveman [strength]|ponytail [strength]] "
    "· strength: lite, full, ultra; Caveman also supports wenyan-*"
)
STRENGTHS = {"lite": "Gentle", "full": "Standard", "ultra": "Strong"}


def handle_lite_mode_command(args: str = "") -> str:  # noqa: PLR0911 - Command grammar.
    """Apply a complete, validated preference change without invoking a model."""
    parts = args.strip().lower().split()
    if not parts or parts == ["settings"]:
        return "Use /lite-mode settings in the TUI to choose a style and strength."
    if len(parts) > 2:  # noqa: PLR2004 - Command grammar.
        return USAGE
    action = parts[0]
    if action in {"on", "off", "status"} and len(parts) != 1:
        return USAGE
    try:
        config = NovaConfig()
        style = config.get_preferred_compression_mode()
        level = None
        if action in {"caveman", "ponytail"}:
            style = action
            level = parts[1] if len(parts) > 1 else None
        elif action in CAVEMAN_LEVELS and len(parts) == 1:
            level = action
        elif action not in {"on", "off", "status"}:
            return USAGE
        if level is not None:
            allowed = CAVEMAN_LEVELS if style == "caveman" else tuple(STRENGTHS)
            if level not in allowed:
                return USAGE
            config.set_caveman_options(
                config.get_caveman_feature_preferences(),
                level if style == "caveman" else config.get_caveman_level(),
                mode=style,
                ponytail_level=level if style == "ponytail" else config.get_ponytail_level(),
            )
        elif action != "status":
            config.set_compression_mode("off" if action == "off" else style)
        active = config.get_compression_mode()
        level = config.get_caveman_level() if style == "caveman" else config.get_ponytail_level()
        strength = STRENGTHS[level.removeprefix("wenyan-")]
        if level.startswith("wenyan-"):
            strength += " · classical Chinese replies"
        label = "Caveman" if style == "caveman" else "Ponytail"
        status = (
            f"Lite Mode: {label} · {strength}."
            if active != "off"
            else f"Lite Mode: off. Saved style: {label} · {strength}."
        )
    except (OSError, ValueError):
        return "Could not save Lite Mode preferences. Your active settings are unchanged."
    return status


def register_commands(registry: CommandRegistry) -> None:
    """Expose the same preference semantics to command and remote consumers."""
    from novacode_cli.config.config import console

    async def handler(ctx: CommandContext) -> bool:
        console.print(handle_lite_mode_command(ctx.cmd_args or ""), markup=False)
        return True

    registry.register("lite-mode", handler)
