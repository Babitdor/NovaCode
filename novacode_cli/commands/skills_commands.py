"""Registry entry for the ``/skills`` command.

Skill management is a TUI surface: ``/skills`` opens ``SkillsScreen``
(``novacode_cli/tui/screens.py``), which lists, toggles, creates, prunes and
restores skills natively. This module keeps the ``/skills`` registration so the
command exists in the registry, and points at that screen.

The console implementation (``prompt_toolkit`` numbered menus and their
``_*_interactive`` helpers) was removed with the console REPL. All the shared
logic lives in UI-free modules, so the TUI and this stub cannot drift:

- ``novacode_cli/skills/prune.py``        — archive / restore / candidates
- ``novacode_cli/skills/skills_prefs.py`` — per-scope enable/disable
- ``novacode_cli/skills/load.py``         — listing and lookup
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.config.config import console

if TYPE_CHECKING:
    from novacode_cli.commands import CommandRegistry


async def handle_skills_command(cmd_args: str | None, assistant_id: str) -> bool:  # noqa: ARG001
    """Explain that skill management is interactive in the TUI."""
    console.print()
    console.print("[yellow]Skill management is interactive in the TUI.[/yellow]")
    console.print(
        "[dim]Use /skills to open the Skills Manager — list, toggle, create, "
        "prune and restore.[/dim]"
    )
    console.print()
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Register the ``/skills`` command."""
    # Imported here (not at module scope) to avoid a circular import:
    # novacode_cli.commands imports this module while building the registry.
    from novacode_cli.commands import CommandContext  # noqa: TC001

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_skills_command(cmd_args=ctx.cmd_args, assistant_id=ctx.assistant_id)

    registry.register("skills", _handle)
