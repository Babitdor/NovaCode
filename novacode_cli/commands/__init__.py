"""Commands package for Novacode CLI.

All slash commands are registered here via ``CommandRegistry`` and invoked
through a single dispatch in ``commands.py``.  Each handler module exports a
``register_commands(registry)`` function that calls ``registry.register(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from novacode_cli.states.Session import SessionState
    from novacode_cli.ui.ui_elements import TokenTracker


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class CommandContext:
    """Context passed to every command handler.

    Handlers pick only the fields they need; the rest are ignored.
    """

    cmd: str
    cmd_args: str | None
    agent: Any
    token_tracker: TokenTracker
    session_state: SessionState
    assistant_id: str
    session_manager: Any = None
    model_name: str | None = None
    image_tracker: Any = None
    sandbox_id: str | None = None
    sandbox_type: str | None = None


CommandHandler = Callable[[CommandContext], Awaitable[str | bool]]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CommandRegistry:
    """Registry mapping command names to their handler functions."""

    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}

    def register(self, name: str, handler: CommandHandler) -> None:
        """Register *handler* for command *name* (without leading ``/``)."""
        self._handlers[name] = handler

    def get(self, name: str) -> CommandHandler | None:
        """Look up a handler by command name.  Returns ``None`` if unknown."""
        return self._handlers.get(name)

    @property
    def commands(self) -> list[str]:
        """Sorted list of all known command names."""
        return sorted(self._handlers)


# ---------------------------------------------------------------------------
# Plugin commands
# ---------------------------------------------------------------------------


def _make_plugin_command_handler(
    plugin_handler: Callable[[str], Awaitable[str]],
) -> CommandHandler:
    """Adapt a plugin's ``async (args) -> str`` into a registry CommandHandler."""

    async def _handler(ctx: CommandContext) -> str | bool:
        try:
            return await plugin_handler(ctx.cmd_args or "")
        except Exception as exc:  # noqa: BLE001
            return f"Plugin command '/{ctx.cmd}' failed: {exc}"

    return _handler


def _register_plugin_commands(registry: CommandRegistry) -> None:
    """Register slash commands from enabled plugins (built-ins take precedence)."""
    try:
        from novacode_cli.plugins.loader import (
            collect_plugin_commands,
            discover_enabled_plugins,
        )

        for name, cmd in collect_plugin_commands(discover_enabled_plugins()).items():
            if registry.get(name) is not None:
                continue  # never shadow a built-in command
            handler = cmd.get("handler")
            if handler is not None:
                registry.register(name, _make_plugin_command_handler(handler))
    except Exception:  # noqa: BLE001 — a bad plugin must not break the CLI
        import logging

        logging.getLogger("nova.plugins").exception(
            "Failed to register plugin commands"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_command_registry() -> CommandRegistry:
    """Create and populate the global command registry.

    Each handler module below should export a ``register_commands(registry)``
    function that registers its command(s).
    """
    registry = CommandRegistry()

    # Module-based handlers ##################################################
    # Import each handler module's register_commands and call it.
    from novacode_cli.commands.agents_commands import register_commands as _r8
    from novacode_cli.commands.browser_use_handler import register_commands as _r14
    from novacode_cli.commands.chat_handler import register_commands as _r19
    from novacode_cli.commands.create_handler import register_commands as _r23
    from novacode_cli.commands.dream_handler import register_commands as _r15
    from novacode_cli.commands.evolution_handler import register_commands as _r21
    from novacode_cli.commands.file_commands import register_commands as _r9
    from novacode_cli.commands.hooks_handler import register_commands as _r4
    from novacode_cli.commands.init_handler import register_commands as _r1
    from novacode_cli.commands.log_commands import register_commands as _r18
    from novacode_cli.commands.mcp_handler import register_commands as _r2
    from novacode_cli.commands.model_handler import register_commands as _r3
    from novacode_cli.commands.notifications_handler import register_commands as _r10
    from novacode_cli.commands.plan_handler import register_commands as _r11
    from novacode_cli.commands.plugins_handler import register_commands as _r20
    from novacode_cli.commands.ralph_handler import register_commands as _r13
    from novacode_cli.commands.research_handler import register_commands as _r16
    from novacode_cli.commands.server_commands import register_commands as _r6
    from novacode_cli.commands.session_commands import register_commands as _r5
    from novacode_cli.commands.skills_commands import register_commands as _r7
    from novacode_cli.commands.trace_handler import register_commands as _r12
    from novacode_cli.commands.trello_handler import register_commands as _r17
    from novacode_cli.commands.wiki_commands import register_commands as _r22
    from novacode_cli.commands.effort_handler import register_commands as _r24
    from novacode_cli.commands.plugin_install_handler import register_commands as _r25
    from novacode_cli.commands.vision_handler import register_commands as _r26

    for _r in (
        _r1,
        _r2,
        _r3,
        _r4,
        _r5,
        _r6,
        _r7,
        _r8,
        _r9,
        _r10,
        _r11,
        _r12,
        _r13,
        _r14,
        _r15,
        _r16,
        _r17,
        _r18,
        _r19,
        _r20,
        _r21,
        _r22,
        _r23,
        _r24,
        _r25,
        _r26,
    ):
        _r(registry)

    # Plugin-contributed commands last, so built-ins always win on collision.
    _register_plugin_commands(registry)
    _register_claude_plugin_commands(registry)

    return registry


def _register_claude_plugin_commands(registry: CommandRegistry) -> None:
    """Register ``commands/*.md`` from installed Claude-compatible plugins.

    Each becomes ``/<name>``; invoking it feeds the markdown body (with
    ``$ARGUMENTS`` substituted) to the agent as a prompt. Built-ins win on a
    name collision.
    """
    try:
        from novacode_cli.plugins.claude_plugins import plugin_commands
    except Exception:  # noqa: BLE001 — plugins are optional
        return

    for cname, _desc, body in plugin_commands():
        if registry.get(cname) is not None:
            continue

        def _make(b: str) -> CommandHandler:
            async def _handler(ctx: CommandContext) -> str:
                args = (ctx.cmd_args or "").strip()
                return b.replace("$ARGUMENTS", args).replace("{{args}}", args)

            return _handler

        registry.register(cname, _make(body))
