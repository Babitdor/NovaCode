"""Browser mode's session-scoped capability boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.agents.tool_search_variants import SEARCH_VARIANTS
from novacode_cli.config.plan_mode import active_plan_session

if TYPE_CHECKING:
    from collections.abc import Mapping

# The shared agent loop binds this live session for every turn, not just planning.
# Reuse that invocation context so concurrent sessions cannot change each other.
RESEARCH_TOOLS = (
    frozenset(
        {
            "web_search",
            "duckduckgo_search",
            "docs_search",
            "fetch_url",
            "search_tools",
            "tool_search",
            "skills_search",
            "skills_load",
            "skills_read_resource",
            "ask_user_question",
            "write_todos",
            "think",
        }
    )
    | SEARCH_VARIANTS
)
BROWSER_ACTIONS = frozenset(
    {
        "navigate",
        "snapshot",
        "screenshot",
        "pdf",
        "click",
        "fill",
        "type",
        "press_key",
        "select_option",
        "evaluate",
        "wait_for",
        "network_requests",
        "console_messages",
        "close",
        "back",
        "forward",
        "reload",
        "markdown",
        "links",
        "extract",
        "interactive_elements",
        "detect_forms",
        "get_attribute",
        "count",
        "search",
        "fill_form",
        "scroll",
        "wait_for_text",
        "get_cookies",
        "set_cookie",
        "clear_cookies",
        "storage_state",
        "set_storage_state",
        "tab_new",
        "tab_list",
        "tab_switch",
        "tab_close",
    }
)
# Raw MCP operation identities, independent of server aliases and descriptions.
# Node/shell dispatch, browser installation and local file upload are excluded.
BROWSER_MCP_TOOLS = frozenset(f"browser_{action}" for action in BROWSER_ACTIONS) | frozenset(
    {
        "browser_navigate_back",
        "browser_tabs",
        "browser_hover",
        "browser_drag",
        "browser_resize",
        "browser_handle_dialog",
        # Chrome DevTools MCP's page-scoped operations.
        "new_page",
        "list_pages",
        "select_page",
        "close_page",
        "navigate_page",
        "wait_for",
        "click",
        "fill",
        "fill_form",
        "press_key",
        "hover",
        "drag",
        "resize_page",
        "take_screenshot",
        "take_snapshot",
        "evaluate_script",
        "list_network_requests",
        "get_network_request",
        "list_console_messages",
        "get_console_message",
        "emulate",
        "performance_start_trace",
        "performance_stop_trace",
        "performance_analyze_insight",
    }
)


def trusted_browser_servers() -> frozenset[str]:
    """Read the saved selection without discovering, installing or enabling servers."""
    from novacode_cli.config.nova_config import NovaConfig

    return frozenset(NovaConfig().get_browser_mode_servers())


def browser_mode_enabled() -> bool:
    """Return the invoking session's live mode flag."""
    return getattr(active_plan_session.get(), "browser_mode_enabled", False) is True


def browser_tool_allowed(
    name: str,
    metadata: Mapping[str, object] | None = None,
    *,
    trusted_servers: frozenset[str] | None = None,
) -> bool:
    """Require explicit server trust and a reviewed browser operation identity."""
    trusted = trusted_browser_servers() if trusted_servers is None else trusted_servers
    if metadata and ("nova_mcp_server" in metadata or "nova_mcp_tool" in metadata):
        server, raw = metadata.get("nova_mcp_server"), metadata.get("nova_mcp_tool")
        return (
            isinstance(server, str)
            and isinstance(raw, str)
            and server in trusted
            and raw in BROWSER_MCP_TOOLS
            and name == f"{server}_{raw}"
        )
    # Preserve the original Obscura names for graph adapters without metadata.
    return name in RESEARCH_TOOLS or (
        "obscura" in trusted
        and name.startswith("obscura_browser_")
        and name.removeprefix("obscura_browser_") in BROWSER_ACTIONS
    )


def browser_mode_args(value: str | list[str] | None) -> tuple[str, str]:
    """Parse a toggle, explicit state, status query, or task without changing its text."""
    text = " ".join(value) if isinstance(value, list) else (value or "")
    text = text.strip()
    if not text:
        return "toggle", ""
    if text.lower() in {"on", "off", "status", "--status", "settings"}:
        return text.lower().removeprefix("--"), ""
    return "on", text
