"""Configure Nova's stacked guardrails from the command line."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novacode_cli.commands import CommandContext, CommandRegistry
    from novacode_cli.security.guardrails import Guardrails

COMMAND = "/guardrails"

_USAGE = (
    "Usage: /guardrails [on|off|status]\n"
    "       /guardrails review on|off\n"
    "       /guardrails type [<type> <strategy>|remove <type>]\n"
    "       /guardrails keyword [add <term>|remove <term>|clear]"
)

#: ``/guardrails type <name> <strategy>`` and ``... type remove <name>``.
_TYPE_ARG_COUNT = 3

_PII_TYPE_USAGE = "Usage: /guardrails type <type> <strategy>|remove <type>"


def _rules_text(guardrails: Guardrails, *, empty: str) -> str:
    return ", ".join(f"{name}={strategy}" for name, strategy in guardrails.pii_types) or empty


def _status_lines(guardrails: Guardrails) -> list[str]:
    if guardrails.pii_enabled:
        pii = (
            f"[green]on[/green] ({_rules_text(guardrails, empty='no types selected')}); "
            f"input={guardrails.apply_to_input}, "
            f"output={guardrails.apply_to_output}, "
            f"tools={guardrails.apply_to_tool_results}"
        )
    else:
        pii = "[yellow]off[/yellow]"
    keywords = ", ".join(guardrails.blocked_keywords) if guardrails.blocked_keywords else "none"
    review = "[green]on[/green]" if guardrails.safety_review_enabled else "[yellow]off[/yellow]"
    return [
        f"PII handling: {pii}",
        f"Blocked request terms: {keywords}",
        f"Model output review: {review}",
        "Tool approvals: enforced by the existing policy and HITL gate (not duplicated here).",
    ]


def _handle_type(guardrails: Guardrails, parts: list[str]) -> tuple[Guardrails, bool]:
    """Apply ``/guardrails type`` and report whether settings changed."""
    from novacode_cli.config.config import console
    from novacode_cli.security.guardrails import PII_STRATEGIES, PII_TYPES

    if len(parts) == 1:
        console.print(f"PII types: {_rules_text(guardrails, empty='none')}")
        console.print(f"Types: {', '.join(PII_TYPES)}")
        console.print(f"Strategies: {', '.join(PII_STRATEGIES)}")
        return guardrails, False
    if len(parts) < _TYPE_ARG_COUNT:
        console.print(_PII_TYPE_USAGE)
        return guardrails, False
    if parts[1].lower() == "remove":
        console.print(f"Removed PII type {parts[2]}.")
        return guardrails.without_pii_type(parts[2]), True
    try:
        updated = guardrails.with_pii_type(parts[1], parts[2])
    except ValueError as error:
        console.print(str(error), markup=False)
        return guardrails, False
    console.print(f"PII type {parts[1]} now uses {parts[2]}.")
    return updated, True


def _handle_keyword(guardrails: Guardrails, parts: list[str]) -> tuple[Guardrails, bool]:
    """Apply ``/guardrails keyword`` and report whether settings changed."""
    from novacode_cli.config.config import console

    sub = parts[1].lower() if len(parts) > 1 else "list"
    term = " ".join(parts[2:]).strip()
    if sub == "add":
        if not term:
            console.print("Usage: /guardrails keyword add <term>")
            return guardrails, False
        console.print(f"Blocked request term added: {term}")
        return guardrails.with_keywords([*guardrails.blocked_keywords, term]), True
    if sub == "remove":
        if not term:
            console.print("Usage: /guardrails keyword remove <term>")
            return guardrails, False
        keywords = [
            keyword
            for keyword in guardrails.blocked_keywords
            if keyword.casefold() != term.casefold()
        ]
        console.print(f"Blocked request term removed: {term}")
        return guardrails.with_keywords(keywords), True
    if sub == "clear":
        console.print("Blocked request terms cleared.")
        return guardrails.with_keywords(()), True
    if sub == "list":
        keywords = guardrails.blocked_keywords or ("none",)
        console.print("Blocked request terms: " + ", ".join(keywords))
    else:
        console.print(_USAGE)
    return guardrails, False


async def handle_guardrails_command(ctx: CommandContext) -> bool:
    """Read or change guardrail settings; rebuild the agent after a change."""
    from novacode_cli.config.config import console
    from novacode_cli.config.nova_config import NovaConfig

    cfg = NovaConfig()
    guardrails = cfg.get_guardrails()
    parts = (ctx.cmd_args or "status").split()
    action = parts[0].lower() if parts else "status"
    changed = False

    if action in {"on", "off"}:
        guardrails = guardrails.with_pii(enabled=action == "on")
        changed = True
        console.print(f"PII handling {'enabled' if action == 'on' else 'disabled'}.")
    elif action == "review":
        value = parts[1].lower() if len(parts) > 1 else "status"
        if value in {"on", "off"}:
            guardrails = guardrails.with_review(enabled=value == "on")
            changed = True
            console.print(f"Model output review {'enabled' if value == 'on' else 'disabled'}.")
        elif value == "status":
            console.print(
                f"Model output review is {'on' if guardrails.safety_review_enabled else 'off'}."
            )
        else:
            console.print(_USAGE)
            return True
    elif action == "type":
        guardrails, changed = _handle_type(guardrails, parts)
    elif action == "keyword":
        guardrails, changed = _handle_keyword(guardrails, parts)
    elif action != "status":
        console.print(_USAGE)
        return True

    if changed:
        cfg.set_guardrails(guardrails)
        if ctx.session_state is not None:
            from novacode_cli.config.model_create import create_model

            try:
                await ctx.session_state.switch_model(create_model())
            except Exception as error:  # noqa: BLE001 - config is already saved
                console.print(
                    f"Guardrails saved, but the live agent could not be rebuilt: {error}",
                    markup=False,
                )
                return True
        console.print("Guardrails saved. The next turn uses the new settings.")

    for line in _status_lines(guardrails):
        console.print(line)
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Register ``/guardrails``."""
    registry.register("guardrails", handle_guardrails_command)
