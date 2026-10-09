"""Streaming constants and helper functions for execution display.

This module contains:
- Tool icons and category mappings
- Internal context keywords for filtering
- Helper functions for activity formatting

Shared between the TUI, headless mode, and server mode.
"""

import re
from pathlib import Path


# Tool icons for display - Beautiful and visually distinct
TOOL_ICONS: dict[str, str] = {
    # File operations - Document themed
    "read_file": "📄",
    "write_file": "✍️",
    "edit_file": "✂️",
    "ls": "📂",
    "glob": "🔍",
    "grep": "🔎",
    # Serena MCP tools (capitalized names)
    "Read": "📄",
    "Write": "✍️",
    "Edit": "✂️",
    "List": "📂",
    "Glob": "🔍",
    "Grep": "🔎",
    "Search": "🔍",
    # Shell - Action themed
    "shell": "⚡",
    "execute": "🔧",
    # Web - Network themed
    "web_search": "🔍",
    "duckduckgo_search": "🦆",
    "fetch_url": "🌐",
    # Task - Agent themed
    "task": "🤖",
    # Todos - Planning themed
    "write_todos": "✅",
    # Server management - DevOps themed
    "start_dev_server": "🚀",
    "stop_server": "🛑",
    "list_servers": "📋",
    # Test runner - Quality themed
    "run_tests": "🧪",
    # Memory - Storage themed
    "write_memory": "💾",
    "read_memory": "📖",
    "list_memories": "📑",
    "delete_memory": "🗑️",
    # Code quality - Analysis themed
    "lint_code": "🔍",
    "check_types": "🔎",
    "package_info": "📦",
    # Question/Plan - Interaction themed
    "ask_question": "❓",
    "exit_plan_mode": "✅",
    # Browser - Web automation themed
    "browser_navigate": "🌐",
    "browser_click": "👆",
    "browser_type": "⌨️",
    "browser_screenshot": "📸",
    "browser_query": "❓",
    "browser_get_content": "📄",
    "browser_get_url": "🔗",
    # Recovery - Restore themed
    "list_trash": "🗑️",
    "restore_file": "♻️",
    # Git tools - Version control themed
    "git_status": "📊",
    "git_log": "📝",
    "git_diff": "🔀",
    "git_blame": "🔍",
}

# Tool category icons for summary display
TOOL_CATEGORY_ICONS: dict[str, str] = {
    "files_read": "📖",
    "files_written": "✏️",
    "search": "🔎",
    "web": "🌐",
    "shell": "⚡",
    "tests": "🧪",
    "server": "🚀",
    "git": "📊",
    "browser": "🌐",
    "memory": "💾",
    "other": "🔧",
}

# Tool category mapping for summary categorization
TOOL_CATEGORIES: dict[str, str] = {
    # Files read
    "read_file": "files_read",
    "ls": "files_read",
    "glob": "files_read",
    "grep": "files_read",
    # Serena MCP tools (capitalized names)
    "Read": "files_read",
    "List": "files_read",
    "Glob": "files_read",
    "Grep": "files_read",
    "Search": "files_read",
    # Files written
    "write_file": "files_written",
    "edit_file": "files_written",
    # Serena MCP tools (capitalized names)
    "Write": "files_written",
    "Edit": "files_written",
    # Search
    "web_search": "search",
    "duckduckgo_search": "search",
    "docs_search": "search",
    # Web
    "fetch_url": "web",
    # Recovery
    "list_trash": "recovery",
    "restore_file": "recovery",
    # Shell
    "shell": "shell",
    "execute": "shell",
    "execute_bash": "shell",
    # Tests
    "run_tests": "tests",
    # Server
    "start_dev_server": "server",
    "stop_server": "server",
    "list_servers": "server",
    # Browser
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_screenshot": "browser",
    "browser_type": "browser",
    "browser_query": "browser",
    "browser_fill_form": "browser",
    "browser_upload": "browser",
    # Memory
    "write_memory": "memory",
    "read_memory": "memory",
    "list_memories": "memory",
    "delete_memory": "memory",
    # Git
    "git_status": "git",
    "git_log": "git",
    "git_diff": "git",
    "git_blame": "git",
}

# Category display order for summary
CATEGORY_ORDER = [
    "files_read",
    "files_written",
    "search",
    "web",
    "shell",
    "tests",
    "server",
    "git",
    "browser",
    "memory",
]

# Internal context keywords the LLM writes as scratchpad text before tool calls.
# Matched case-insensitively after stripping any leading markdown heading markers (# ## ###).
# These are not user-facing and should be stripped from display.
INTERNAL_CONTEXT_KEYWORDS = (
    "extracted context",
    "context summary",
    "current goal",
    "current task",
    "task context",
    "planning context",
    "internal context",
    "working context",
    "session intent",
    "session context",
    "session summary",
    "agent context",
    "agent state",
    # LLM-generated project preamble before tool calls (e.g. "**Project:** Nova-Code...")
    "project:",
    "project overview",
)


def format_condensed_activity(activity: dict, max_items: int = 3) -> str:
    """Format a condensed activity summary for display.

    Shows key files/operations in a compact format:
    - File paths: "auth.py, utils.py, +2 more"
    - Categories: "🔎 3 searches • 🌐 2 web"
    """
    parts = []

    # Show file operations with paths (most useful)
    files_read = activity.get("files_read", [])
    if files_read:
        # Get just filenames, not full paths
        filenames = [Path(f).name for f in files_read[:max_items]]
        remaining = activity.get("files_read_count", len(files_read)) - max_items
        if remaining > 0:
            parts.append(f"📖 {', '.join(filenames)}, +{remaining}")
        else:
            parts.append(f"📖 {', '.join(filenames)}")

    files_written = activity.get("files_written", [])
    if files_written:
        filenames = [Path(f).name for f in files_written[:max_items]]
        remaining = activity.get("files_written_count", len(files_written)) - max_items
        if remaining > 0:
            parts.append(f"✏️ {', '.join(filenames)}, +{remaining}")
        else:
            parts.append(f"✏️ {', '.join(filenames)}")

    # Show other categories as counts
    categories = activity.get("categories", {})
    for cat in CATEGORY_ORDER:
        if cat in ("files_read", "files_written"):
            continue  # Already handled above
        count = categories.get(cat, 0)
        if count > 0:
            icon = TOOL_CATEGORY_ICONS.get(cat, "🔧")
            parts.append(f"{icon} {count}")

    # Show errors count
    error_count = activity.get("error_count", len(activity.get("errors", [])))
    if error_count:
        parts.append(f"❌ {error_count}")

    return " • ".join(parts) if parts else "starting..."


def get_tool_icon(tool_name: str) -> str:
    """Get the icon for a tool name."""
    return TOOL_ICONS.get(tool_name, "🔧")


def get_tool_category(tool_name: str) -> str:
    """Get the category for a tool name."""
    return TOOL_CATEGORIES.get(tool_name, "other")


# Section headings of the summary emitted by langchain's SummarizationMiddleware
# (DEFAULT_SUMMARY_PROMPT). Its output is ordinary assistant prose, so the only
# way to recognize it is by this structure.
# "Session Intent" is the tell: no real answer opens a section with it, whereas
# "Summary" / "Artifacts" / "Next steps" are all headings a genuine answer uses
# (an answer with BOTH "## Summary" and "## Next steps" is ordinary, and keying
# on section-count alone silently swallowed it).
_REQUIRED_SUMMARY_SECTION = "session intent"
_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}|\*\*|__)\s*([^\n#*_]{1,60})", re.MULTILINE)


# Headings unique to the resume briefing that build_continuation_prompt puts in
# the system message (see novacode_cli/session/session_prompt_builder.py). A
# model on a resumed session sometimes recites that briefing back as its first
# reply, which reads as the assistant dumping its own system prompt. None of
# these is a heading a genuine answer would write, so any ONE is conclusive —
# unlike "Summary"/"Next steps", which real answers legitimately use.
_CONTINUATION_SECTIONS = frozenset(
    {
        "continuation mode",
        "current workspace state",
        "session memory",
        "task state",
    }
)


def looks_like_continuation_briefing(text: str) -> bool:
    """True if *text* is the resume briefing echoed back at the user.

    The briefing is a SystemMessage and is never replayed into the transcript
    directly; this catches the model *reproducing* it as assistant prose.
    """
    if text.lstrip().startswith("<identity>"):
        return True
    for m in _HEADING_RE.finditer(text):
        head = m.group(1).strip().rstrip(":").lower()
        if head in _CONTINUATION_SECTIONS:
            return True
    return False


def looks_like_summarization_output(text: str) -> bool:
    """True if *text* is a SummarizationMiddleware summary.

    Matches on the section structure ANYWHERE in the text, not just as a
    prefix: models routinely precede the block with a lead-in ("Here is the
    extracted context:"), wrap it in a code fence, or emit the sections out of
    order, and a prefix-only check let every one of those through to the
    transcript.
    """
    for m in _HEADING_RE.finditer(text):
        head = m.group(1).strip().rstrip(":").lower()
        if head == _REQUIRED_SUMMARY_SECTION:
            return True
    return False


def _could_be_session_intent_heading(text: str) -> bool:
    """Hold a short heading prefix so internal summaries never flash in the UI."""
    first_line = text.splitlines()[0] if text else ""
    candidate = first_line.lstrip().lstrip("#*_ \t").strip().lower()
    return not candidate or "session intent".startswith(candidate)


def is_internal_context_text(text: str) -> bool:
    """Check if text is internal context/scratchpad content.

    Normalizes by stripping leading markdown heading markers (# / ## / ###) and
    bold markers (** / __) and whitespace, then checks case-insensitively against
    known internal keywords. Also matches a summarization block appearing after
    a lead-in or fence (see :func:`looks_like_summarization_output`) and the
    resume briefing echoed back (see :func:`looks_like_continuation_briefing`).
    """
    stripped = text.lstrip()
    heading_stripped = stripped.lstrip("#*_").lstrip()
    if any(
        heading_stripped.lower().startswith(kw)
        for kw in INTERNAL_CONTEXT_KEYWORDS
    ):
        return True
    return looks_like_summarization_output(text) or looks_like_continuation_briefing(text)
