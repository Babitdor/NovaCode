"""Load rarely used tools and subagents on demand, not on every model call.

Nova binds ~85 tools (~117k chars of schemas, most of them MCP: playwright,
serena, apify, chrome-devtools) and the ``task`` tool lists ~90 subagents
(~19k chars), all resent on every call. This follows Anthropic's Tool Search
Tool (defer_loading: tokens -85%, Opus tool accuracy 49% -> 74%), done
provider-side-agnostically in middleware:

* Only **core** tools (file, shell, todos, plan mode, search), the user's
  **most-used** tools (Hermes ``tool_stats``, read once per session) and tools
  already **loaded** in this thread are bound.
* Every subagent except the always-listed general-purpose one drops out of the
  ``task`` description the same way.
* ``search_tools`` finds the rest (hybrid search, shared with skills) and loads
  them: a tool is loaded once a ``search_tools`` result names it or the model
  has called it. Derived from the messages, so it survives checkpoints and
  never needs its own state. Loaded tools stay loaded (a changing tool list
  re-bills the prompt cache, so it should change rarely).

Every tool stays registered with the graph, so a call to one that is not bound
still executes; only the schemas sent to the model shrink.
"""

from __future__ import annotations

import logging
import re
from difflib import get_close_matches
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from novacode_cli.skills.retrieval import first_sentence, get_index

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

logger = logging.getLogger("nova.tool_search")

#: Always bound: the tools a coding turn cannot do without.
CORE_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "shell",
        "execute",
        "bash",
        "eval",
        "write_todos",
        "task",
        "think",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
        "web_search",
        "fetch_url",
        "code_search",
        "skills_search",
        "search_tools",
        "memory_search",
        # The system prompt delegates background work proactively. Keep the
        # entire available async lifecycle visible without a search round-trip.
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
        # Artifacts: the prompt tells the agent to create these proactively, so
        # the schemas must be bound without a search_tools round-trip — otherwise
        # the instruction names tools the model cannot see.
        "create_artifact",
        "update_artifact",
        "list_artifacts",
        # Desktop power. Same rule as the artifacts above: the prompt tells the
        # agent to end an overnight session by suspending the machine, so the tool
        # has to be bound without a `search_tools` round-trip.
        "sleep_desktop",
    }
)
#: Also bound: up to this many of the user's most-used other tools...
FREQUENT_TOOLS = 10
#: ...that have been called at least this often.
FREQUENT_MIN_USES = 20

LOADED_MARKER = "Loaded tools:"
MAX_SEARCH_LIMIT = 50


def _name(tool: Any) -> str:
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("function", {}).get("name", "")
    return getattr(tool, "name", "")


def _description(tool: Any) -> str:
    if isinstance(tool, dict):
        return tool.get("description") or tool.get("function", {}).get("description", "")
    return getattr(tool, "description", "") or ""


def loaded_names(messages: list[Any]) -> set[str]:
    """Tools this thread has loaded: named by ``search_tools`` or already called."""
    names: set[str] = set()
    for msg in messages:
        if isinstance(msg, AIMessage):
            names.update(tc["name"] for tc in msg.tool_calls or [])
        elif isinstance(msg, ToolMessage) and msg.name in ("search_tools", "tool_search"):
            texts = (
                [msg.content]
                if isinstance(msg.content, str)
                else [
                    part if isinstance(part, str) else part.get("text", "") for part in msg.content
                ]
            )
            for text in texts:
                for line in text.splitlines():
                    if line.startswith(LOADED_MARKER):
                        names.update(n.strip() for n in line[len(LOADED_MARKER) :].split(","))
    return names


def _group(names: list[str]) -> str:
    """``playwright_browser_click, playwright_browser_close`` -> one line per prefix."""
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        prefix = name.split("_", 1)[0] if "_" in name else ""
        groups.setdefault(prefix, []).append(name)
    lines = []
    for prefix, members in groups.items():
        if prefix and len(members) >= 3:
            short = ", ".join(m[len(prefix) + 1 :] for m in members)
            lines.append(f"- `{prefix}_*`: {short}")
        else:
            lines.extend(f"- `{m}`" for m in members)
    return "\n".join(lines)


class ToolSearchMiddleware(AgentMiddleware):
    """Bind core + frequent + loaded tools; ``search_tools`` loads the rest."""

    def __init__(
        self,
        *,
        deferred_subagents: dict[str, str] | None = None,
        async_subagents: set[str] | None = None,
    ) -> None:
        """``deferred_subagents``: name -> description, hidden from ``task`` until found."""
        super().__init__()
        self._deferred_subagents = deferred_subagents or {}
        self._async_subagents = async_subagents or set()
        self._frequent: set[str] | None = None  # read once, then frozen
        self._note: str | None = None  # frozen: a changing prompt re-bills the cache
        self._inventory: tuple = ()
        self._catalog: list[dict] = []
        self.tools = [
            StructuredTool.from_function(
                self._tool_search,
                name="search_tools",
                description=(
                    "Find and load any available tool or subagent, including loaded tools. "
                    "Search by capability, exact names separated by commas, partial names, "
                    "or wildcards such as playwright_*. Use query='*' to browse the catalog. "
                    "Use limit and offset to page through results. Found tools are callable "
                    "from your next step on."
                ),
            )
        ]

    # ── search_tools ───────────────────────────────────────────────────────

    def _search_matches(self, query: str) -> list[dict]:
        """Rank the full catalog, retaining exact names ahead of semantic hits."""
        tokens = re.findall(r"[\w.:-]+", query.casefold())
        names = {item["name"].casefold() for item in self._catalog}
        exact = [item for item in self._catalog if item["name"].casefold() in tokens]
        patterns = [
            word.strip("`\"'") for word in re.split(r"[,\s]+", query.casefold()) if "*" in word
        ]
        if patterns:
            return [
                item
                for item in self._catalog
                if item in exact
                or any(fnmatchcase(item["name"].casefold(), pattern) for pattern in patterns)
            ]
        if tokens and set(tokens) <= names:
            return exact
        normalized = re.sub(r"[^\w]+|_", " ", query.casefold()).strip()
        partial = [
            item
            for item in self._catalog
            if normalized
            and normalized in re.sub(r"[^\w]+|_", " ", item["name"].casefold()).strip()
        ]
        fuzzy_names = get_close_matches(query.casefold(), sorted(names), n=3, cutoff=0.8)
        fuzzy = [item for item in self._catalog if item["name"].casefold() in fuzzy_names]
        try:
            ranked = [
                item for item, _, _ in get_index(self._catalog).search(query, k=len(self._catalog))
            ]
        except Exception:  # noqa: BLE001 - discovery still works without the optional index
            logger.debug("Tool retrieval index unavailable; using lexical search", exc_info=True)
            ranked = [
                item
                for item in self._catalog
                if set(tokens) & set(re.findall(r"[\w]+", item["description"].casefold()))
            ]
        hits: dict[tuple[str, str], dict] = {}
        for item in [*exact, *partial, *fuzzy, *ranked]:
            hits.setdefault((item["kind"], item["name"]), item)
        return list(hits.values())

    def _tool_search(self, query: str, limit: int = 5, offset: int = 0) -> str:
        """Find and load available tools and subagents.

        Args:
            query: A capability, exact or partial names, or a wildcard; '*' lists everything.
            limit: Results per page (1-50).
            offset: Number of matches to skip to reach another page.
        """
        if not 1 <= limit <= MAX_SEARCH_LIMIT or offset < 0:
            message = "limit must be between 1 and 50, and offset must be nonnegative."
            raise ValueError(message)
        query = query.strip()
        if not query:
            return (
                "Enter a capability or tool name, or use query='*' to browse all available tools."
            )
        if not self._catalog:
            return "No tools are currently registered for discovery."
        matches = self._search_matches(query)
        if not matches:
            return (
                f"No registered tools match {query!r}. "
                "Try a shorter name, a server prefix, or query='*' to browse."
            )
        hits = matches[offset : offset + limit]
        if not hits:
            return (
                f"There are {len(matches)} matches. Retry with offset smaller than {len(matches)}."
            )
        tools = [h for h in hits if h["kind"] == "tool"]
        agents = [h for h in hits if h["kind"] == "subagent"]
        lines = [f"- `{h['name']}`: {first_sentence(h['description'], 200)}" for h in tools]
        for agent in agents:
            name = agent["name"]
            dispatch = "start_async_task" if name in self._async_subagents else "task"
            lines.append(
                f'- subagent `{name}` (use `{dispatch}(subagent_type="{name}")`): '
                f"{first_sentence(agent['description'], 200)}"
            )
        loaded = ", ".join(h["name"] for h in hits)
        result = f"{LOADED_MARKER} {loaded}\n" + "\n".join(lines)
        if offset or len(matches) > len(hits):
            result += f"\nShowing {offset + 1}-{offset + len(hits)} of {len(matches)} matches."
            if offset + len(hits) < len(matches):
                result += (
                    f" Continue with the same query, limit={limit}, offset={offset + len(hits)}."
                )
        return result

    # ── binding ────────────────────────────────────────────────────────────

    async def _read_frequent(self, request: ModelRequest) -> None:
        if self._frequent is not None:
            return
        self._frequent = set()
        store = getattr(request.runtime, "store", None)
        if store is None:
            return
        try:
            items = await store.asearch(("nova", "tool_stats"), limit=1000)
        except Exception:  # noqa: BLE001 — ranking is a nicety, never break a turn
            logger.debug("tool_stats unavailable", exc_info=True)
            return
        uses = {i.key: int((i.value or {}).get("uses", 0)) for i in items}
        ranked = sorted((n for n in uses if n not in CORE_TOOLS), key=lambda n: -uses[n])
        self._frequent = {n for n in ranked[:FREQUENT_TOOLS] if uses[n] >= FREQUENT_MIN_USES}

    def _trim_task(self, tool: Any, hidden: list[str]) -> Any:
        """Drop hidden subagents from the ``task`` description (a request-local copy).

        One pass over the description's lines: with the whole roster deferred
        (~90 names) a per-name regex would rescan the whole string 90 times on
        every model call. Matching the ``- name:`` prefix keeps the agent name
        authoritative even when a description contains regex metacharacters.
        """
        hidden_set = set(hidden)
        kept: list[str] = []
        for line in _description(tool).splitlines():
            match = re.match(r"^[ \t]*- ([^:]+):", line)
            if match and match.group(1).strip() in hidden_set:
                continue
            kept.append(line)
        text = "\n".join(kept)
        text += (
            f"\n\n{len(hidden)} more specialist subagents are not listed here: "
            "`search_tools` finds them by what they do."
        )
        return tool.model_copy(update={"description": text})

    def _apply(self, request: ModelRequest) -> ModelRequest:
        # Discovery covers the full available inventory, regardless of which
        # schemas this particular model call already has loaded.
        self._catalog = [
            {"name": _name(t), "description": _description(t), "kind": "tool"}
            for t in request.tools
            if _name(t)
        ] + [
            {"name": n, "description": description, "kind": "subagent"}
            for n, description in self._deferred_subagents.items()
        ]
        inventory = tuple(
            (item["kind"], item["name"], item["description"]) for item in self._catalog
        )
        if inventory != self._inventory:
            self._note = None
            self._inventory = inventory
        loaded = loaded_names(request.messages)
        keep = CORE_TOOLS | (self._frequent or set()) | loaded
        bound, deferred = [], []
        for tool in request.tools:
            (bound if _name(tool) in keep else deferred).append(tool)
        hidden = [n for n in self._deferred_subagents if n not in loaded]
        if hidden:
            bound = [
                self._trim_task(t, hidden) if _name(t) == "task" and not isinstance(t, dict) else t
                for t in bound
            ]
        if not deferred and not hidden:
            return request

        if self._note is None:  # changes only when available inventory changes
            optional_names = [
                item["name"]
                for item in self._catalog
                if item["kind"] == "tool" and item["name"] not in CORE_TOOLS
            ]
            self._note = (
                "\n\n## More tools (load on demand)\n\n"
                f"Additional tool schemas load on demand to save context. "
                f"{len(self._deferred_subagents)} specialist subagents are searchable.\n"
                f"{_group(optional_names)}\n\n"
                'Call `search_tools("<what you need>")` or `search_tools("name1, name2")`; '
                'Search covers all available tools. Use query="*" or a wildcard '
                'such as "playwright_*" '
                "to browse; limit and offset retrieve further matches. "
                "What it returns is callable from your next step. Never guess a tool's "
                "arguments: load it first."
            )
        blocks = request.system_message.content_blocks if request.system_message else []
        return request.override(
            tools=bound,
            system_message=SystemMessage(content=[*blocks, {"type": "text", "text": self._note}]),
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Bind only core, frequent and loaded tools (sync: no usage ranking)."""
        if self._frequent is None:
            self._frequent = set()
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Bind only core, frequent and loaded tools."""
        await self._read_frequent(request)
        return await handler(self._apply(request))
