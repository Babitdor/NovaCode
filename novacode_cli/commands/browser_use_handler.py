"""Toggle browser research on the shared agent instead of launching a separate agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.config.browser_mode import browser_mode_args
from novacode_cli.config.config import console

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from novacode_cli.commands import CommandContext, CommandRegistry
    from novacode_cli.states.Session import SessionState
    from novacode_cli.ui.ui_elements import TokenTracker


async def handle_browser_use_command(
    agent: Any,  # noqa: ANN401 -- Shared command accepts the caller's compiled agent.
    session_state: SessionState,
    assistant_id: str,
    token_tracker: TokenTracker,
    cmd_args: str | list[str] | None,
    *,
    execute_fn: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Toggle, report status, or start a task using the existing agent and model."""
    action, task = browser_mode_args(cmd_args)
    if action == "status":
        console.print(f"Browser Mode: {'on' if session_state.browser_mode_enabled else 'off'}")
        return True
    session_state.browser_mode_enabled = (
        not session_state.browser_mode_enabled if action == "toggle" else action == "on"
    )
    console.print(
        "Browser Mode on: Obscura and web research."
        if session_state.browser_mode_enabled
        else "Browser Mode off. Findings remain in this conversation."
    )
    if task:
        if execute_fn is None:
            from novacode_cli.ui.execution import execute_task

            execute_fn = execute_task
        await execute_fn(
            user_input=task,
            agent=agent,
            assistant_id=assistant_id,
            session_state=session_state,
            token_tracker=token_tracker,
        )
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Expose the same mode command to non-TUI callers."""

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_browser_use_command(
            ctx.agent,
            ctx.session_state,
            ctx.assistant_id,
            ctx.token_tracker,
            ctx.cmd_args,
        )

    registry.register("browser-use", _handle)
