"""Tools package for novacode_cli.

This package provides various tools for the CLI agent, organized into submodules:
- web_tools: Web search tools (Tavily, DuckDuckGo, docs search)
- fetch_tools: URL fetching and content conversion (merged from http_tools)
- memory_tools: Memory management tools
- reflection_tools: Reflection tools for strategic thinking
- graph_tools: Project knowledge-graph query tool
- code_search_tools: Semantic code search (Semble-powered, optional)
"""

# URL fetching tools (covers all HTTP methods — http_request merged into fetch_url)
from novacode_cli.tools.fetch_tools import fetch_url

# Memory tools (markdown files injected into the prompt)
from novacode_cli.tools.memory_tools import (
    read_memory,
    write_memory,
)

# Structured, durable, cross-session memory (key/value via the LangGraph store)
from novacode_cli.tools.store_memory_tools import (
    forget,
    list_memories,
    recall,
    remember,
)

# Code search tools (Semble-powered, optional — gracefully degrades)
try:
    from novacode_cli.tools.code_search_tools import (
        code_search,
        find_related_code,
        _is_semble_available as _semble_avail,
    )
except ImportError:
    code_search = None  # type: ignore[assignment]
    find_related_code = None  # type: ignore[assignment]
    _semble_avail = lambda: False  # type: ignore[assignment]

# Oracle (multi-model fusion)
from novacode_cli.tools.oracle_tool import oracle

# Reflection tools
from novacode_cli.tools.reflection_tools import think

# Speak tool
from novacode_cli.tools.speak_tool import speak

# Desktop power (suspend the machine when the user is done for the night)
from novacode_cli.tools.desktop_tools import sleep_desktop
from novacode_cli.tools.notebook_tools import notebook_edit

# Skill management (agent-facing write path for self-improvement)
from novacode_cli.tools.skill_tools import skill_manage

# Web search tools
from novacode_cli.tools.web_tools import (
    docs_search,
    duckduckgo_search,
    web_search,
)

# Wiki tools (agent-accessible project wiki)
from novacode_cli.tools.wiki_tools import (
    wiki_read,
    wiki_search,
    wiki_update_index,
    wiki_write,
)

__all__ = [
    "docs_search",
    "duckduckgo_search",
    # URL fetching tools
    "fetch_url",
    "read_memory",
    # Structured durable memory (LangGraph store)
    "remember",
    "recall",
    "list_memories",
    "forget",
    # Reflection tools
    "oracle",
    "think",
    # Speak tool
    "speak",
    # Desktop power: the overnight "park the machine" action
    "sleep_desktop",
    # Skill management (agent self-improvement write path)
    "skill_manage",
    # Web search tools
    "web_search",
    # Memory tools
    "write_memory",
    # Code search tools (Semble-powered, optional)
    "code_search",
    "find_related_code",
    # Wiki tools (agent-accessible project wiki)
    "wiki_read",
    "wiki_search",
    "wiki_update_index",
    "wiki_write",
]
