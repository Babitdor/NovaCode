"""Handler for the /vision command — configure image handling.

Nova routes images through an auxiliary ``vision_model`` when the main model is
text-only. When the main model is multimodal, images go straight to it. This
command shows and edits both settings, so a user no longer has to hand-edit
``~/.nova/Nova.config.json``.
"""

from __future__ import annotations

from novacode_cli.commands import CommandContext
from novacode_cli.config.config import COLORS, console
from novacode_cli.config.model_capabilities import resolve_main_model_multimodal
from novacode_cli.config.nova_config import NovaConfig


def _current_main_model() -> tuple[str, str]:
    """Return ``(provider, model)`` for the configured main model."""
    cfg = NovaConfig().get_model_config()
    if isinstance(cfg, dict):
        return cfg.get("provider", ""), cfg.get("model", "")
    return "", ""


def _print_status() -> None:
    """Print the current vision configuration."""
    nova_config = NovaConfig()
    _, model = _current_main_model()
    override = nova_config.get_main_model_multimodal()
    # The same resolver the agent uses, so the status line cannot claim
    # "no" while the agent passes images through (or the reverse).
    detected = resolve_main_model_multimodal(model)
    vision_cfg = nova_config.get_vision_model_config()

    console.print()
    console.print("[bold]Vision Configuration[/bold]", style=COLORS["primary"])
    console.print(f"  Main model:        [bold cyan]{model or '(unset)'}[/bold cyan]")
    console.print(
        f"  Main model sees images: "
        f"[bold cyan]{'yes' if detected else 'no'}[/bold cyan]"
        + (
            " [dim](forced)[/dim]"
            if override is not None
            else " [dim](auto-detected)[/dim]"
        )
    )
    if detected:
        console.print(
            "  [green]Images are sent directly to the main model "
            "(no captioning).[/green]"
        )
    else:
        console.print(
            f"  Auxiliary vision model: "
            f"[bold cyan]{vision_cfg['provider']}/{vision_cfg['model']}[/bold cyan]"
        )
        console.print("  [dim]Images are captioned to text by the vision model.[/dim]")
    console.print()
    console.print("[bold]Usage:[/bold]")
    console.print("  /vision                       - Show this status")
    console.print("  /vision <provider> <model>    - Set the auxiliary vision model")
    console.print("  /vision on                    - Force: main model is multimodal")
    console.print("  /vision off                   - Force: main model is text-only")
    console.print("  /vision auto                  - Auto-detect from the model name")
    console.print()


async def handle_vision_command(ctx: CommandContext) -> bool:
    """Handle the /vision command."""
    args = (ctx.cmd_args or "").strip()
    nova_config = NovaConfig()

    if not args:
        _print_status()
        return True

    parts = args.split()
    first = parts[0].lower()

    # ── Override: on / off / auto ───────────────────────────────────────────
    if first in ("on", "off", "auto"):
        value = {"on": True, "off": False, "auto": None}[first]
        nova_config.set_main_model_multimodal(value)
        console.print()
        if value is None:
            console.print(
                "[green]✓ Main-model multimodal override cleared "
                "(auto-detect).[/green]"
            )
        else:
            console.print(
                f"[green]✓ Main model forced to "
                f"{'multimodal' if value else 'text-only'}.[/green]"
            )
        console.print("[dim]Takes effect on the next agent build (restart).[/dim]")
        console.print()
        return True

    # ── Set the auxiliary vision model: /vision <provider> <model> ──────────
    if len(parts) >= 2:
        provider, model = parts[0], " ".join(parts[1:])
        nova_config.set_vision_model_config(provider, model)
        console.print()
        console.print(
            f"[green]✓ Vision model set to {provider}/{model}.[/green]"
        )
        console.print("[dim]Takes effect on the next agent build (restart).[/dim]")
        console.print()
        return True

    console.print(
        "[red]Error: expected '/vision <provider> <model>', "
        "'/vision on|off|auto', or no arguments.[/red]"
    )
    return True


def register_commands(registry) -> None:
    async def _handle(ctx: CommandContext) -> bool:
        return await handle_vision_command(ctx)

    registry.register("vision", _handle)
