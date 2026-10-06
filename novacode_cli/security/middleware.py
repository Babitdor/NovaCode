"""Agent middleware that screens URL-bearing tool arguments for deception.

Before a tool runs, URL-like arguments (``url``/``uri``/``href``/… — see
:data:`URL_ARG_KEYS`) are checked for:

- hidden/invisible Unicode (BiDi overrides, zero-width chars, BOM, …)
- punycode domain spoofing (``xn--`` decoding)
- mixed-script / confusable domain labels

Policy is **warn + sanitize**: invisible characters are stripped from the URL in
place and the call proceeds; spoofing signals are surfaced as a notice but not
blocked. This is intentionally limited to URL args — blanket sanitization of all
tool arguments would risk mutating legitimate file content (BOMs, combining
marks, non-Latin text).

Notices are emitted through the shared Nova event buffer (drained by the agent
loop into a ``ContextMessage`` rendered by both the Rich REPL and the Textual
TUI). They are never ``console.print``-ed — printing from middleware corrupts the
TUI.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware

from novacode_cli.security.unicode_security import (
    URL_ARG_KEYS,
    check_url_safety,
    format_warning_detail,
    strip_dangerous_unicode,
)

logger = logging.getLogger("nova.security.middleware")


def _emit_security_event(message: str) -> None:
    """Surface a security notice via the shared Nova event buffer (TUI-safe)."""
    try:
        from novacode_cli.events import nova_event_log

        nova_event_log.append(("nova_security", "⛨", "yellow", message))
    except Exception:  # noqa: BLE001
        logger.debug("security notice (not surfaced): %s", message)


class SecurityMiddleware(AgentMiddleware):
    """Warn + sanitize deceptive Unicode / spoofed domains in URL tool args."""

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        try:
            args = request.tool_call.get("args")
            if isinstance(args, dict):
                self._screen_url_args(args, request.tool_call.get("name", "tool"))
        except Exception:  # noqa: BLE001 — screening must never break the call
            logger.debug("URL security screen failed", exc_info=True)
        return await handler(request)

    @staticmethod
    def _screen_url_args(args: dict[str, Any], tool_name: str) -> None:
        """Strip hidden Unicode from URL args in place; warn on spoofing."""
        for key, value in list(args.items()):
            if not isinstance(value, str) or not value:
                continue
            if key.lower() not in URL_ARG_KEYS:
                continue

            result = check_url_safety(value)
            sanitized = strip_dangerous_unicode(value)
            if sanitized != value:
                args[key] = sanitized  # mutate the live tool_call args

            if not result.safe:
                detail = format_warning_detail(result.warnings) or "suspicious URL"
                _emit_security_event(
                    f"{tool_name}: proceeding with sanitized URL — {detail}"
                )
            elif sanitized != value:
                _emit_security_event(
                    f"{tool_name}: stripped hidden Unicode from '{key}'"
                )
