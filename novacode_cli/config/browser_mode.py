"""Browser mode's session-scoped capability boundary."""

from __future__ import annotations

from novacode_cli.agents.tool_search_variants import SEARCH_VARIANTS
from novacode_cli.config.plan_mode import active_plan_session

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


def browser_mode_enabled() -> bool:
    """Return the invoking session's live mode flag."""
    return getattr(active_plan_session.get(), "browser_mode_enabled", False) is True


def browser_tool_allowed(name: str) -> bool:
    """Allow reviewed web tools and Obscura actions; unknown capabilities fail closed."""
    return name in RESEARCH_TOOLS or (
        name.startswith("obscura_browser_")
        and name.removeprefix("obscura_browser_") in BROWSER_ACTIONS
    )


def browser_mode_args(value: str | list[str] | None) -> tuple[str, str]:
    """Parse a toggle, explicit state, status query, or task without changing its text."""
    text = " ".join(value) if isinstance(value, list) else (value or "")
    text = text.strip()
    if not text:
        return "toggle", ""
    if text.lower() in {"on", "off", "status", "--status"}:
        return text.lower().removeprefix("--"), ""
    return "on", text
