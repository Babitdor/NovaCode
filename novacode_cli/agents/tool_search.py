"""Load rarely used tools and subagents on demand, not on every model call.

Tool schemas and specialist rosters load on demand, independently of provider:

* Only **core** tools (file, shell, todos, plan mode, search), the user's
  **most-used** tools (Hermes ``tool_stats``, read once per session) and tools
  already **loaded** in this thread are bound.
* Every subagent except the always-listed general-purpose one drops out of the
  ``task`` description the same way.
* ``search_tools`` finds the rest (hybrid search, shared with skills) and loads
  them: a tool is loaded once a ``search_tools`` result names it or the model
  has called it. Successful structured results are matched to their calls and
  retained in checkpoints. Compaction drops visibility when that evidence leaves
  context; rediscovery restores it. Search uses the owning execution's inventory.

Every tool stays registered with the graph, so a call to one that is not bound
still executes; only the schemas sent to the model shrink.
"""

from __future__ import annotations

import logging
import re
import time
from difflib import get_close_matches
from fnmatch import fnmatchcase
from functools import partial, update_wrapper
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime  # noqa: TC002 - Pydantic resolves runtime annotations.
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict

from novacode_cli.agents.tool_search_variants import (
    BM25_TOOL,
    REGEX_TOOL,
    SEARCH_VARIANTS,
    BM25Search,
    SearchInput,
    parameter_text,
    regex_matches,
)
from novacode_cli.skills.retrieval import MIN_COSINE, first_sentence, get_index

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.tools import BaseTool

    from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware

logger = logging.getLogger("nova.tool_search")

#: Always bound: the tools a coding turn cannot do without. Chosen from real
#: use (2026-10 audit: 5,089 calls over 40 threads, 93% of them shell,
#: read_file, edit_file and grep); a tool called a few times a month is cheaper
#: as one search_tools round-trip than as a schema resent on every call.
#: `think`, `code_search`, `web_search`, `eval`, `execute` (locally the same
#: shell as `shell`), `skills_read_resource`, the rarer async and artifact
#: tools and `list_waiters` are found by search.
CORE_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "shell",
        "bash",
        "write_todos",
        "task",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
        "SendMessage",
        "list_sessions",
        "EnterWorktreeTool",
        "ExitWorktreeTool",
        "fetch_url",
        "skills_search",
        "skills_load",
        "search_tools",
        REGEX_TOOL,
        BM25_TOOL,
        "memory_search",
        # The prompt delegates background work proactively and collects it with
        # one short wait, so starting, checking and waiting stay bound.
        "start_async_task",
        "check_async_task",
        "wait_for_task",
        # The prompt asks for artifacts proactively.
        "create_artifact",
        # Surface the opt-in guest and its readiness without loading input schemas.
        "computer_status",
        "knowledge_lookup",
    }
)
#: Never bound by use alone: `execute` duplicates `shell` locally (both run
#: through Nova's shell), so a model that used it is better steered to `shell`.
NEVER_FREQUENT = frozenset({"execute"})
#: Also bound: up to this many of the user's most-used other tools...
FREQUENT_TOOLS = 10
#: ...called at least this often in the last FREQUENT_DAYS days. All-time counts
#: kept tools from a month-old workflow (Serena, Playwright) on every call.
FREQUENT_MIN_USES = 20
FREQUENT_DAYS = 30

#: Shorter descriptions for third-party tools whose defaults are long (the
#: rules live in the system prompt). ``write_todos``: deepagents' 3.9k-char
#: text, which also contradicts Nova's one-item-in-progress rule.
DESCRIPTIONS = {
    "write_todos": (
        "Create or replace the task list for multi-step work: a list of todos, each "
        "with `content` and a `status` of pending, in_progress or completed. Follow "
        "the rules in <todo_management>. Never call it twice in parallel."
    ),
}

LOADED_MARKER = "Loaded tools:"
#: Tools whose result can load others with a LOADED_MARKER line: the search,
#: and create_artifact, whose natural next step is update_artifact.
LOADING_TOOLS = frozenset({"search_tools", "tool_search", "create_artifact"}) | SEARCH_VARIANTS
MAX_SEARCH_LIMIT = 50
MAX_INVENTORY_CHARS = 2_000
MAX_INVENTORY_GROUPS = 10
MAX_GROUP_CHARS = 1_000
LOAD_RECORD = "nova_tool_search"


class RuntimeSearchInput(SearchInput):
    """Runtime inventory is injected by the graph, never supplied by the model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime


def _name(tool: Any) -> str:
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("function", {}).get("name", "")
    return getattr(tool, "name", "")


def _description(tool: Any) -> str:
    if isinstance(tool, dict):
        return tool.get("description") or tool.get("function", {}).get("description", "")
    return getattr(tool, "description", "") or ""


def _with_description(tool: BaseTool | dict, description: str) -> BaseTool | dict:
    """Copy descriptions without mutating registered objects or function schemas."""
    if isinstance(tool, dict):
        if isinstance(tool.get("function"), dict):
            return {**tool, "function": {**tool["function"], "description": description}}
        return {**tool, "description": description}
    return tool.model_copy(update={"description": description})


def loaded_capabilities(messages: list[Any]) -> set[tuple[str, str]]:
    """Typed schema visibility from calls and their successful structured results.

    Old text-only discovery reports remain readable, but require rediscovery.
    A marker embedded in tool output is never a loading instruction.
    """
    loaded: set[tuple[str, str]] = set()
    calls: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for call in msg.tool_calls or []:
                call_id = call.get("id")
                if isinstance(call_id, str):
                    calls[call_id] = call["name"]
                loaded.add(("tool", call["name"]))
                if call["name"] in {"task", "start_async_task"}:
                    agent = call.get("args", {}).get("subagent_type")
                    if isinstance(agent, str):
                        loaded.add(("subagent", agent))
        elif isinstance(msg, ToolMessage):
            expected = calls.pop(msg.tool_call_id, None)
            if (
                msg.status == "error"
                or msg.name not in LOADING_TOOLS
                or expected != msg.name
                or not isinstance(msg.artifact, dict)
            ):
                continue
            record = msg.artifact.get(LOAD_RECORD)
            if (
                not isinstance(record, dict)
                or type(record.get("version")) is not int
                or record["version"] != 1
            ):
                continue
            capabilities = record.get("capabilities")
            if not isinstance(capabilities, list):
                continue
            for capability in capabilities:
                if (
                    isinstance(capability, dict)
                    and isinstance(capability.get("kind"), str)
                    and capability.get("kind") in {"tool", "subagent"}
                    and isinstance(capability.get("name"), str)
                    and capability["name"]
                ):
                    loaded.add((capability["kind"], capability["name"]))
    return loaded


def loaded_names(messages: list[Any]) -> set[str]:
    """Compatibility projection; binding uses typed capabilities."""
    return {name for _, name in loaded_capabilities(messages)}


def _group(names: list[str]) -> str:
    """Bounded capability groups; full member names stay behind search."""
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        prefix = name.split("_", 1)[0] if "_" in name else ""
        groups.setdefault(prefix, []).append(name)
    lines: list[str] = []
    shown = 0
    for prefix, members in groups.items():
        label = f"{prefix}_*" if prefix else "other names"
        line = f"- `{label[:80]}` ({len(members)} tools)"
        if len(lines) >= MAX_INVENTORY_GROUPS or sum(map(len, lines)) + len(line) > MAX_GROUP_CHARS:
            break
        lines.append(line)
        shown += len(members)
    if shown < len(names):
        lines.append(f"- {len(names) - shown} further tools; search to inspect.")
    return "\n".join(lines)


class _AccountingRequest:
    """Just enough of a ModelRequest for :func:`sent_tools`."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def override(self, **changes: Any) -> _AccountingRequest:
        return _AccountingRequest(**{**self.__dict__, **changes})


def sent_tools(tools: Sequence[Any], messages: Sequence[Any]) -> list[Any]:
    """The schemas a request binds for ``messages``: selected and trimmed as sent.

    For accounting before any request exists (``/context`` at startup), where the
    registered schemas overstate the request several times: the ``task`` tool
    alone lists every subagent (~39k chars) until this middleware trims it. The
    deferred roster is read back from the ``task`` and ``start_async_task``
    descriptions; the user's frequently used tools are left out.
    """
    from novacode_cli.agents.default_subagents.subagents import LISTED_SUBAGENTS

    def roster(name: str) -> list[str]:
        tool = next((t for t in tools if _name(t) == name), None)
        if tool is None:
            return []
        lines = _description(tool).splitlines()
        return [m.group(1) for line in lines if (m := re.match(r"^[ \t]*- ([^:\s]+):", line))]

    middleware = ToolSearchMiddleware(
        deferred_subagents={n: "" for n in roster("task") if n not in LISTED_SUBAGENTS},
        async_subagents=set(roster("start_async_task")),
    )
    middleware._frequent = set()
    request = _AccountingRequest(
        tools=list(tools), messages=list(messages), system_message=SystemMessage(""), state={}
    )
    return list(middleware._apply(request).tools)  # type: ignore[arg-type]


class ToolSearchMiddleware(AgentMiddleware):
    """Bind core + frequent + loaded tools; ``search_tools`` loads the rest."""

    def __init__(
        self,
        *,
        deferred_subagents: dict[str, str] | None = None,
        async_subagents: set[str] | None = None,
        skills: RefreshingSkillsMiddleware | None = None,
    ) -> None:
        """``deferred_subagents``: name -> description, hidden from ``task`` until found."""
        super().__init__()
        self._deferred_subagents = deferred_subagents or {}
        self._async_subagents = async_subagents or set()
        self._skills = skills
        self._frequent: set[str] | None = None  # read once, then frozen
        self._bm25 = BM25Search()
        self.tools = [
            StructuredTool.from_function(
                func=update_wrapper(partial(self._search, mode=mode), self._search),
                coroutine=update_wrapper(partial(self._asearch, mode=mode), self._asearch),
                name=name,
                args_schema=RuntimeSearchInput,
                response_format="content_and_artifact",
                description=description,
            )
            for name, mode, description in (
                (
                    "search_tools",
                    "hybrid",
                    "Find tools/subagents by capability, exact/partial names or wildcards. "
                    "Searches names, descriptions and parameters. query='*' browses; limit and "
                    "offset page results. Found schemas load next step; approvals still apply.",
                ),
                (
                    REGEX_TOOL,
                    "regex",
                    "Find tools/subagents using case-insensitive Python regex over names, "
                    "descriptions and parameters. Maximum pattern length: 200 characters. "
                    "Use query, limit and offset to page results.",
                ),
                (
                    BM25_TOOL,
                    "bm25",
                    "Find tools/subagents using BM25 keywords over names, descriptions and "
                    "parameters without embeddings. Maximum query length: 500 characters. "
                    "Use query, limit and offset to page results.",
                ),
            )
        ]

    def _catalog_for(self, tools: Sequence[Any]) -> list[dict]:
        """Discovery metadata belongs to this invocation's current inventory."""
        return [
            {
                "name": _name(tool),
                "description": _description(tool),
                "kind": "tool",
                "parameters": parameter_text(tool),
            }
            for tool in tools
            if _name(tool)
        ] + [
            {"name": name, "description": description, "kind": "subagent"}
            for name, description in self._deferred_subagents.items()
        ]

    def _search(
        self,
        query: str,
        runtime: ToolRuntime,
        limit: int = 5,
        offset: int = 0,
        *,
        mode: str = "hybrid",
    ) -> tuple[str, dict]:
        """Use the owning run's inventory, including its activated skill aliases."""
        tools = {_name(tool): tool for tool in runtime.tools}
        if self._skills is not None:
            resolved = self._skills._resolve_tools(runtime.state.get("messages", []), runtime)
            for name, tool in resolved.items():
                tools.setdefault(name, tool)
        return self._tool_search(
            query,
            limit,
            offset,
            catalog=self._catalog_for(list(tools.values())),
            _mode=mode,
        )

    async def _asearch(
        self,
        query: str,
        runtime: ToolRuntime,
        limit: int = 5,
        offset: int = 0,
        *,
        mode: str = "hybrid",
    ) -> tuple[str, dict]:
        """Resolve async skill aliases in the same invocation context."""
        tools = {_name(tool): tool for tool in runtime.tools}
        if self._skills is not None:
            resolved = await self._skills._aresolve_tools(
                runtime.state.get("messages", []), runtime
            )
            for name, tool in resolved.items():
                tools.setdefault(name, tool)
        return self._tool_search(
            query,
            limit,
            offset,
            catalog=self._catalog_for(list(tools.values())),
            _mode=mode,
        )

    def _search_matches(self, query: str, catalog: list[dict]) -> list[dict]:
        """Rank the full catalog, retaining exact names ahead of semantic hits."""
        tokens = re.findall(r"[\w.:-]+", query.casefold())
        names = {item["name"].casefold() for item in catalog}
        exact = [item for item in catalog if item["name"].casefold() in tokens]
        patterns = [
            word.strip("`\"'") for word in re.split(r"[,\s]+", query.casefold()) if "*" in word
        ]
        if patterns:
            return [
                item
                for item in catalog
                if item in exact
                or any(fnmatchcase(item["name"].casefold(), pattern) for pattern in patterns)
            ]
        if tokens and set(tokens) <= names:
            return exact
        normalized = re.sub(r"[^\w]+|_", " ", query.casefold()).strip()
        partial = [
            item
            for item in catalog
            if normalized
            and normalized in re.sub(r"[^\w]+|_", " ", item["name"].casefold()).strip()
        ]
        fuzzy_names = get_close_matches(query.casefold(), sorted(names), n=3, cutoff=0.8)
        fuzzy = [item for item in catalog if item["name"].casefold() in fuzzy_names]
        try:
            ranked = [
                item
                for item, cosine, bm25 in get_index(catalog).search(query, k=len(catalog))
                if bm25 > 0 or cosine >= MIN_COSINE
            ]
        except Exception:  # noqa: BLE001 - discovery still works without the optional index
            logger.debug("Tool retrieval index unavailable; using lexical search", exc_info=True)
            ranked = [
                item
                for item in catalog
                if set(tokens)
                & set(
                    re.findall(
                        r"[\w]+",
                        (
                            item["name"]
                            + " "
                            + item["description"]
                            + " "
                            + item.get("parameters", "")
                        ).casefold(),
                    )
                )
            ]
        hits: dict[tuple[str, str], dict] = {}
        for item in [*exact, *partial, *fuzzy, *ranked]:
            hits.setdefault((item["kind"], item["name"]), item)
        return list(hits.values())

    def _tool_search(
        self,
        query: str,
        limit: int = 5,
        offset: int = 0,
        *,
        catalog: list[dict],
        _mode: str = "hybrid",
    ) -> tuple[str, dict]:
        """Find and load available tools and subagents.

        Args:
            query: A capability, exact or partial names, or a wildcard; '*' lists everything.
            limit: Results per page (1-50).
            offset: Number of matches to skip to reach another page.
            catalog: Metadata from the owning invocation's current tool inventory.
        """
        if not 1 <= limit <= MAX_SEARCH_LIMIT or offset < 0:
            message = "limit must be between 1 and 50, and offset must be nonnegative."
            raise ValueError(message)
        query = query if _mode == "regex" else query.strip()
        if not query:
            return (
                "Enter a capability or tool name, or use query='*' to browse all available tools."
            ), {}
        if not catalog:
            return "No tools are currently registered for discovery.", {}
        try:
            if _mode == "regex":
                matches = regex_matches(catalog, query)
            elif _mode == "bm25":
                matches = self._bm25.search(catalog, query)
            else:
                matches = self._search_matches(query, catalog)
        except (ValueError, re.error, TimeoutError) as exc:
            return f"Tool search failed: {exc}", {}
        if not matches:
            return (
                f"No registered tools match {query!r}. "
                "Try a shorter name, a server prefix, or query='*' to browse."
            ), {}
        hits = matches[offset : offset + limit]
        if not hits:
            return (
                f"There are {len(matches)} matches. Retry with offset smaller than {len(matches)}."
            ), {}
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
        return result, {
            LOAD_RECORD: {
                "version": 1,
                "capabilities": [{"kind": hit["kind"], "name": hit["name"]} for hit in hits],
            }
        }

    # ── binding ────────────────────────────────────────────────────────────

    async def _read_frequent(self, request: ModelRequest) -> None:
        """Rank tools by recent use: Hermes's per-session ``tool_history``."""
        if self._frequent is not None:
            return
        self._frequent = set()
        store = getattr(request.runtime, "store", None)
        if store is None:
            return
        since = time.time() - FREQUENT_DAYS * 86400
        uses: dict[str, int] = {}
        try:
            spaces = await store.alist_namespaces(
                prefix=("nova",), suffix=("tool_history",), limit=1000
            )
            # Filtered here too: Nova's stdlib fallback store ignores prefix/suffix.
            for space in (s for s in spaces if s[:1] == ("nova",) and s[-1:] == ("tool_history",)):
                item = await store.aget(space, "history")
                for entry in ((item.value or {}).get("entries") or []) if item else []:
                    name = entry.get("tool")
                    if name and float(entry.get("timestamp") or 0) >= since:
                        uses[name] = uses.get(name, 0) + 1
        except Exception:  # noqa: BLE001 — ranking is a nicety, never break a turn
            logger.debug("tool_history unavailable", exc_info=True)
            return
        ranked = sorted(
            (n for n in uses if n not in CORE_TOOLS and n not in NEVER_FREQUENT),
            key=lambda n: -uses[n],
        )
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
        skip_lines = 0
        for line in _description(tool).splitlines():
            if skip_lines:
                skip_lines -= 1
                continue
            match = re.match(r"^[ \t]*- ([^:]+):", line)
            if match and match.group(1).strip() in hidden_set:
                name = match.group(1).strip()
                # deepagents inserts each source description verbatim after
                # the roster heading. Remove its complete block, not just line 1.
                skip_lines = max(0, len(self._deferred_subagents[name].splitlines()) - 1)
                continue
            kept.append(line)
        text = "\n".join(kept)
        text += (
            f"\n\n{len(hidden)} more specialist subagents are not listed here: "
            "`search_tools` finds them by what they do."
        )
        return _with_description(tool, text)

    def _compact_async_roster(self, tool: Any) -> Any:
        """One line per agent in ``start_async_task``'s roster (a request-local copy).

        deepagents lists each async agent with its whole description, "Use this
        agent when:" bullets included (~6.5k chars for 13 agents). The names and
        first sentences keep proactive delegation possible; ``search_tools``
        returns any agent's full description.
        """
        lines, in_roster = [], False
        for line in _description(tool).splitlines():
            if line.startswith("Available async agent types"):
                in_roster = True
                lines.append(line)
                continue
            if in_roster and line.startswith("#"):
                in_roster = False
                lines.append("")
            if not in_roster:
                lines.append(line)
                continue
            match = re.match(r"^- ([^:\s]+): (.*)", line)
            if match and match.group(1) in self._async_subagents:
                lines.append(f"- {match.group(1)}: {first_sentence(match.group(2), 160)}")
        return _with_description(tool, "\n".join(lines))

    def _shorten(self, tool: Any, hidden: list[str]) -> Any:
        """The request-local copy of a bound tool with its description trimmed."""
        name = _name(tool)
        if name == "task" and hidden:
            return self._trim_task(tool, hidden)
        if name == "start_async_task" and self._async_subagents:
            return self._compact_async_roster(tool)
        if name in DESCRIPTIONS:
            return _with_description(tool, DESCRIPTIONS[name])
        return tool

    def _apply(self, request: ModelRequest) -> ModelRequest:
        capabilities = loaded_capabilities(request.messages)
        loaded = {name for kind, name in capabilities if kind == "tool"}
        loaded_agents = {name for kind, name in capabilities if kind == "subagent"}
        # Skill snapshots activate their optional schemas without a second search.
        # Ordinary registered tools remain independently searchable.
        for message in request.messages:
            extra = getattr(message, "additional_kwargs", {})
            snapshot = extra.get("skill", {}) if extra.get("lc_source") == "pinned_skill" else {}
            if (
                isinstance(message, ToolMessage)
                and message.name == "skills_load"
                and message.status != "error"
                and isinstance(message.artifact, dict)
            ):
                snapshot = message.artifact.get("skill", {})
            if isinstance(snapshot, dict):
                include = snapshot.get("include_tools", [])
                if isinstance(include, list):
                    loaded.update(name for name in include if isinstance(name, str))
        keep = set(CORE_TOOLS) | (self._frequent or set()) | loaded
        keep.update(
            _name(tool)
            for tool in request.tools
            if (getattr(tool, "metadata", None) or {}).get("_nova_skill_active")
        )
        bound: list[Any] = []
        deferred: list[Any] = []
        for tool in request.tools:
            (bound if _name(tool) in keep else deferred).append(tool)
        hidden = [n for n in self._deferred_subagents if n not in loaded_agents]
        bound = [self._shorten(t, hidden) for t in bound]
        if not deferred and not hidden:
            return request.override(tools=bound)

        optional_names = [
            _name(tool) for tool in request.tools if _name(tool) and _name(tool) not in CORE_TOOLS
        ]
        note = (
            "\n\n## More tools (load on demand)\n\n"
            f"{len(optional_names)} optional tools and {len(self._deferred_subagents)} "
            "specialist subagents are searchable. Capability groups:\n"
            f"{_group(optional_names)}\n\n"
            'Call search_tools("<capability>") or use exact names. query="*" browses; '
            'wildcards such as "playwright_*", limit and offset inspect further results. '
            f"{REGEX_TOOL} supports regex; {BM25_TOOL} supports keyword search. "
            "Search loads schemas, not permission. Normal approvals and execution policy "
            "still apply. Never guess arguments: load the schema first. "
            "After compaction, search again for schemas no longer available."
        )[:MAX_INVENTORY_CHARS]
        blocks = (
            [dict(block) for block in request.system_message.content_blocks]
            if request.system_message
            else []
        )
        return request.override(
            tools=bound,
            system_message=SystemMessage(content=[*blocks, {"type": "text", "text": note}]),
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
