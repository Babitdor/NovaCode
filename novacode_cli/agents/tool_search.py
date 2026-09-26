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
        "check_async_task",
        # Artifacts: the prompt tells the agent to create these proactively, so
        # the schemas must be bound without a search_tools round-trip — otherwise
        # the instruction names tools the model cannot see.
        "create_artifact",
        "update_artifact",
        "list_artifacts",
    }
)
#: Also bound: up to this many of the user's most-used other tools...
FREQUENT_TOOLS = 10
#: ...that have been called at least this often.
FREQUENT_MIN_USES = 20

LOADED_MARKER = "Loaded tools:"


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
            for line in str(msg.content).splitlines():
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

    def __init__(self, *, deferred_subagents: dict[str, str] | None = None) -> None:
        """``deferred_subagents``: name -> description, hidden from ``task`` until found."""
        super().__init__()
        self._deferred_subagents = deferred_subagents or {}
        self._frequent: set[str] | None = None  # read once, then frozen
        self._note: str | None = None  # frozen: a changing prompt re-bills the cache
        self._catalog: list[dict] = []
        self.tools = [
            StructuredTool.from_function(
                self._tool_search,
                name="search_tools",
                description=(
                    "Find and load tools or subagents that are available but not loaded yet "
                    "(see 'More tools' in your instructions). Pass what you need to do, "
                    "or exact tool names separated by commas. Loaded tools are callable "
                    "from your next step on."
                ),
            )
        ]

    # ── search_tools ───────────────────────────────────────────────────────

    def _tool_search(self, query: str) -> str:
        """Find and load deferred tools and subagents.

        Args:
            query: What you need to do, or exact tool names separated by commas.
        """
        by_name = {item["name"]: item for item in self._catalog}
        exact = [by_name[n.strip()] for n in re.split(r"[,\s]+", query) if n.strip() in by_name]
        hits = exact or [item for item, _, _ in get_index(self._catalog).search(query, k=5)]
        if not hits:
            return f"Nothing matches {query!r}. Use the tools you already have."
        tools = [h for h in hits if h["kind"] == "tool"]
        agents = [h for h in hits if h["kind"] == "subagent"]
        lines = [f"- `{h['name']}`: {first_sentence(h['description'], 200)}" for h in tools]
        lines += [
            f'- subagent `{h["name"]}` (use `task(subagent_type="{h["name"]}")`): '
            f"{first_sentence(h['description'], 200)}"
            for h in agents
        ]
        loaded = ", ".join(h["name"] for h in hits)
        return f"{LOADED_MARKER} {loaded}\n" + "\n".join(lines)

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

        self._catalog = [
            {"name": _name(t), "description": _description(t), "kind": "tool"} for t in deferred
        ] + [
            {"name": n, "description": self._deferred_subagents[n], "kind": "subagent"}
            for n in hidden
        ]
        if self._note is None:  # the full deferrable set, frozen for the session
            self._note = (
                "\n\n## More tools (load on demand)\n\n"
                f"These {len(deferred)} tools are available but not loaded, to save "
                f"context. {len(hidden)} specialist subagents are likewise unlisted.\n"
                f"{_group([_name(t) for t in deferred])}\n\n"
                'Call `search_tools("<what you need>")` or `search_tools("name1, name2")`; '
                "what it returns is callable from your next step. Never guess a tool's "
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
