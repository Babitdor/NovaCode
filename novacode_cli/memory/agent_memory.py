"""Middleware for loading agent-specific long-term memory into the system prompt."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from langchain.tools import ToolRuntime

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import SystemMessage

try:
    from typing import NotRequired
except ImportError:
    from typing import NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage

# from langgraph.runtime import Runtime
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from novacode_cli.config.config import Settings
from novacode_cli.memory.limits import (
    DEFAULT_MEMORY_BLOCK_CHARS,
    DEFAULT_MEMORY_INDEX_CHARS,
    memory_budget,
)
from novacode_cli.prompts import render_template

logger = logging.getLogger(__name__)

# Injection-time truncation keeps the file *head* (newest, given memory files are
# written newest-first — see novacode_cli/memory/limits.py for the invariant).
_MEMORY_TRUNCATION_NOTICE = "\n\n... [memory truncated — use read_file for full content]"
#: The index is a pointer list; the tail is reachable via ``memory_search``.
_INDEX_TRUNCATION_NOTICE = "\n\n... [index truncated — `memory_search` finds the rest]"


class AgentMemoryState(AgentState):
    """State for the agent memory middleware."""

    user_memory: NotRequired[str]
    """Personal preferences from ~/.nova/{agent}/ (applies everywhere)."""

    project_memory: NotRequired[str]
    """Project-specific context (combined from all found memory files)."""

    memory_index: NotRequired[str]
    """The topic-memory index (~/.nova/{agent}/memories/INDEX.md), if present."""

    habits_memory: NotRequired[str]
    """Good-habits surface (~/.nova/{agent}/HABITS.md), always injected."""

    learning_overview: NotRequired[str]
    """Compact at-a-glance view of Nova's learning state (memory topics, skills,
    prompt evolution, recent refinements). See hermes/overview.py."""


class AgentMemoryStateUpdate(TypedDict):
    """A state update for the agent memory middleware."""

    user_memory: NotRequired[str]
    """Personal preferences from ~/.nova/{agent}/ (applies everywhere)."""

    project_memory: NotRequired[str]
    """Project-specific context (combined from all found memory files)."""

    memory_index: NotRequired[str]
    """The topic-memory index (~/.nova/{agent}/memories/INDEX.md), if present."""

    habits_memory: NotRequired[str]
    """Good-habits surface (~/.nova/{agent}/HABITS.md), always injected."""

    learning_overview: NotRequired[str]
    """Compact at-a-glance view of Nova's learning state (memory topics, skills,
    prompt evolution, recent refinements). See hermes/overview.py."""


# Long-term Memory Documentation
# Note: Claude Code loads CLAUDE.md files hierarchically and combines them (not precedence-based):
# - Loads recursively from cwd up to (but not including) root directory
# - Multiple files are combined hierarchically: enterprise → project → user
# - Both [project-root]/CLAUDE.md and [project-root]/.claude/CLAUDE.md are loaded if both exist
# - Files higher in hierarchy load first, providing foundation for more specific memories
# We follow that pattern for Nova CLI
# Long-term memory system prompt is loaded from: NovaCode_cli/prompts/longterm_memory.jinja


DEFAULT_MEMORY_SNIPPET = """<user_memory>
{user_memory}
</user_memory>

<project_memory>
{project_memory}
</project_memory>"""


class AgentMemoryMiddleware(AgentMiddleware):
    """Middleware for loading agent-specific long-term memory.

    This middleware loads the agent's long-term memory from files (CLAUDE.md,
    NOVA.md) and injects them into the system prompt. Memory is loaded once
    at the start of the conversation and stored in state.

    Supports loading from multiple project memory files and combining them.

    When a sandbox backend is provided, memory files are read from the sandbox
    instead of the local filesystem.
    """

    state_schema = AgentMemoryState

    #: Per-block injection budget. Overwritten per instance in ``__init__``
    #: from the model's window; the class default keeps ``_cap`` working for
    #: instances built without it.
    _max_chars: int = DEFAULT_MEMORY_BLOCK_CHARS

    def __init__(
        self,
        *,
        settings: Settings,
        assistant_id: str,
        system_prompt_template: str | None = None,
        skip_project_memory: bool = False,
        backend: Any = None,
        context_window: int = 0,
        memory_block_chars: int | None = None,
        memory_index_chars: int | None = None,
        project_key: str = "",
        model_resolver: Callable[[Mapping[str, Any]], BaseChatModel | None] | None = None,
    ) -> None:
        """Initialize the agent memory middleware.

        Args:
            settings: Global settings instance with project detection and paths.
            assistant_id: The agent identifier.
            system_prompt_template: Optional custom template for injecting
                agent memory into system prompt.
            skip_project_memory: If True, skip loading project memory files
                (NOVA.md/CLAUDE.md). Use on session continuation to avoid
                duplicate context.
            backend: Optional sandbox backend for reading files from sandbox.
                When provided, memory files are read from the sandbox instead
                of the local filesystem.
            context_window: The bound model's window in tokens, used to size the
                per-block injection budget. 0 keeps the fixed legacy cap.
            memory_block_chars: Per-block injection ceiling (agent.md, INDEX,
                HABITS, project memory). Defaults to the configured budget.
            memory_index_chars: Chars of the topic index injected; the rest is
                reachable via ``memory_search``.
            project_key: Key of the codebase this session is in (see
                ``hermes.memory_tiers.project_memory_key``). Lessons recorded
                for that project are retrieved alongside the general ones;
                other projects' lessons are never read.
            model_resolver: Resolve the model selected for the current run,
                including a per-run model-router choice, for explicit search reranking.
        """
        self.settings = settings
        self._project_key = project_key
        self._max_chars = memory_budget(
            context_window, cap=memory_block_chars or DEFAULT_MEMORY_BLOCK_CHARS
        )
        self._index_chars = memory_index_chars or DEFAULT_MEMORY_INDEX_CHARS
        self.assistant_id = assistant_id
        self.skip_project_memory = skip_project_memory
        self._backend = backend
        self._model_resolver = model_resolver

        # User paths
        self.agent_dir = settings.get_agent_dir(assistant_id)
        # Virtual path for LLM-facing display (consistent across OS)
        self.agent_dir_display = f"/memories/"
        self.agent_dir_absolute = f"/memories/"

        # Project paths (from settings)
        self.project_root = settings.project_root

        self.system_prompt_template = system_prompt_template or DEFAULT_MEMORY_SNIPPET

        # Track which project memory files were loaded (for display in prompt)
        self.loaded_project_memory_sources: list[str] = []

        # Track file modification times for hot-reloading
        self._last_mtimes: dict[str, float] = {}

        # Cache for loaded memory content
        self._cached_user_memory: str | None = None
        self._cached_project_memory: str | None = None
        self._cached_memory_index: str | None = None
        self._cached_habits_memory: str | None = None
        self._cached_learning_overview: str | None = None
        # Cache for rendered memory section to avoid re-rendering on every request
        self._memory_section_cache: str | None = None
        self._memory_section_cache_time: float = 0
        self._memory_section_cache_ttl: float = 30.0  # 30 seconds TTL

        from novacode_cli.memory.search import MemorySearchService

        self._memory_search_service = MemorySearchService(
            self.agent_dir,
            project_key=project_key,
        )

        # Progressive disclosure for topic memory: the system prompt carries only
        # a small slice of INDEX.md, and this tool finds the rest (and the topic
        # bodies) on demand. Registered like ``skills_search``.
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._memory_search_tool,
                name="memory_search",
                description=(
                    "Search your long-term topic memory (cross-session decisions, "
                    "facts and lessons) by what you are trying to recall. Uses local "
                    "semantic retrieval and the active model to rerank a small candidate "
                    "set. Returns exact virtual paths for file-backed topics. Read relevant "
                    "full pages for details; activated knowledge may have no page."
                ),
            )
        ]

    def _get_file_mtime(self, path: Path) -> str | None:
        """Compatibility name for current-content identity, including deletion."""
        try:
            from novacode_cli.memory.parsed_cache import file_hash

            return file_hash(path)
        except OSError:
            return None

    def _files_changed(self, paths: list[Path]) -> bool:
        """Check if any files have been modified since last load."""
        current = {str(path): self._get_file_mtime(path) for path in paths}
        return current != self._last_mtimes

    def _record_mtimes(self, paths: list[Path]) -> None:
        """Record modification times for all paths."""
        self._last_mtimes = {str(path): self._get_file_mtime(path) for path in paths}

    def _cap(self, content: str) -> str:
        """Truncate an injected memory block to this model's per-block budget.

        Keeps the HEAD: memory files are written newest-first (see
        ``memory/limits.py``), so the head is the recent content.
        """
        if len(content) <= self._max_chars:
            return content
        return content[: self._max_chars] + _MEMORY_TRUNCATION_NOTICE

    def _cap_index(self, content: str) -> str:
        """Truncate the injected topic index to its own (smaller) budget.

        The index is a pointer list, so a small slice orients the agent and
        ``memory_search`` finds the rest — instead of paying for ~100k chars of
        pointers on every turn.
        """
        if len(content) <= self._index_chars:
            return content
        return content[: self._index_chars] + _INDEX_TRUNCATION_NOTICE

    def _read_file(self, path: Path) -> str | None:
        """Read file content from backend or local filesystem.

        When a CompositeBackend is provided, uses virtual paths to read
        files through the backend's routing system. Falls back to direct
        filesystem read when the backend is unavailable or the read fails.

        Virtual path mapping:
            - User memory: /memories/agent.md
            - Project memory: /project-memory/NOVA.md, /project-memory/CLAUDE.md

        Args:
            path: Real filesystem path to the file to read.

        Returns:
            File content as string, or None if file doesn't exist or can't be read.
        """
        # Try backend with virtual paths when available.
        if self._backend is not None:
            virtual_path = self._real_to_virtual(path)
            if virtual_path:
                try:
                    from novacode_cli.utils.backend_paths import read_via_backend

                    content = read_via_backend(virtual_path, self._backend)
                    if content is not None:
                        return content
                except Exception:
                    pass

        # Local filesystem read (fallback)
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass

        return None

    def _real_to_virtual(self, real_path: Path) -> str | None:
        """Convert a real filesystem path to a virtual path for backend routing.

        Maps known memory directories to their virtual path prefixes:
            - ~/.nova/{agent_id}/ → /memories/
            - {project_root}/.nova/ → /project-memory/

        Args:
            real_path: Real filesystem path.

        Returns:
            Virtual path string, or None if the path doesn't match any route.
        """
        from novacode_cli.utils.backend_paths import real_to_virtual_path

        return real_to_virtual_path(
            real_path,
            agent_id=self.assistant_id,
            workspace_root=self.project_root,
        )

    def _read_file_local(self, path: Path) -> str | None:
        """Read file content from local filesystem only.

        Used for user memory files that are stored locally and not synced to sandbox.

        Args:
            path: Path to the file to read.

        Returns:
            File content as string, or None if file doesn't exist or can't be read.
        """
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass

        return None

    def _read_file_sandbox(self, path: Path) -> str | None:
        """Read file content through backend using virtual paths.

        Used for project memory files that should be read through the backend
        when available. Converts the real path to a virtual path and routes
        through the CompositeBackend.

        Args:
            path: Real filesystem path to the file to read.

        Returns:
            File content as string, or None if file doesn't exist or can't be read.
        """
        if self._backend is not None:
            virtual_path = self._real_to_virtual(path)
            if virtual_path:
                try:
                    from novacode_cli.utils.backend_paths import read_via_backend

                    content = read_via_backend(virtual_path, self._backend)
                    if content is not None:
                        return content
                except Exception:
                    pass

        # Fall back to local filesystem
        return self._read_file_local(path)

    def _file_exists(self, path: Path) -> bool:
        """Check if file exists via backend or local filesystem.

        When a backend is available, uses virtual paths to check existence.
        Falls back to local filesystem check.

        Args:
            path: Real filesystem path to check.

        Returns:
            True if file exists, False otherwise.
        """
        # Try backend with virtual paths when available.
        if self._backend is not None:
            virtual_path = self._real_to_virtual(path)
            if virtual_path:
                try:
                    # Try to read the file through the backend.
                    from novacode_cli.utils.backend_paths import read_via_backend

                    content = read_via_backend(virtual_path, self._backend)
                    return content is not None
                except Exception:
                    pass

        # Local filesystem check
        return path.exists()

    def before_agent(  # type: ignore
        self,
        state: AgentMemoryState,
        # runtime: Runtime,
    ) -> AgentMemoryStateUpdate:
        """Load agent memory from file before agent execution.

        Loads both user agent.md and project-specific memory files if available.
        Project memory is combined from multiple sources (CLAUDE.md, NOVA.md).

        Hot-reload: Automatically reloads memory files when they change on disk.
        Tracks modification times and reloads when files are updated.

        When a sandbox backend is provided:
        - User memory files (~/.nova/{agent}/agent.md) are ALWAYS read from local
          filesystem since they're not synced to the sandbox
        - Project memory files ({project_root}/.nova/NOVA.md) are read from the
          sandbox when available

        Args:
            state: Current agent state.
            runtime: Runtime context.

        Returns:
            Updated state with user_memory and project_memory populated.
        """
        result: AgentMemoryStateUpdate = {}

        # Gather all memory file paths to check for changes
        user_path = self.settings.get_user_agent_md_path(self.assistant_id)
        index_path = self.agent_dir / "memories" / "INDEX.md"
        habits_path = user_path.parent / "HABITS.md"
        project_paths = (
            self.settings.get_project_agent_md_paths() if not self.skip_project_memory else []
        )
        all_paths = [user_path, index_path, habits_path] + list(project_paths)

        # Check if any files have changed (hot-reload) - only for local filesystem
        # In sandbox mode, we always reload since we can't track mtimes
        needs_reload = self._files_changed(all_paths) if self._backend is None else True
        result.update(
            {
                key: ""
                for key in (
                    "user_memory",
                    "project_memory",
                    "memory_index",
                    "habits_memory",
                    "learning_overview",
                )
                if needs_reload
                and key in state
                and (key != "project_memory" or not self.skip_project_memory)
            }
        )

        # Load user memory if not in state or if file changed
        # User memory is read through the backend when available (virtual path: /memories/agent.md),
        # or from local filesystem as fallback.
        if needs_reload or "user_memory" not in state:
            content = self._read_file(user_path)
            if content is not None:
                result["user_memory"] = content

        # Load the topic-memory index (memories/INDEX.md). It's a compact pointer
        # list, so injecting it gives the agent a live map of what it already
        # knows; it then reads the referenced topic files on demand.
        if needs_reload or "memory_index" not in state:
            index_content = self._read_file(index_path)
            if index_content is not None and index_content.strip():
                index_content = self._cap_index(index_content)
                result["memory_index"] = index_content

        # Load the always-injected good-habits file (HABITS.md), if present.
        if needs_reload or "habits_memory" not in state:
            habits_content = self._read_file(habits_path)
            if habits_content is not None and habits_content.strip():
                habits_content = self._cap(habits_content)
                result["habits_memory"] = habits_content

        # Load the compact learning overview (memory topics, skills, prompt
        # evolution, recent refinements). Best-effort; empty when nothing exists.
        if needs_reload or "learning_overview" not in state:
            overview = self._build_learning_overview()
            if overview:
                result["learning_overview"] = overview

        # Load project memory from ALL available sources if not in state or if files changed
        # Project memory is read from sandbox when available, local otherwise
        if not self.skip_project_memory and (needs_reload or "project_memory" not in state):
            combined_memories: list[str] = []
            self.loaded_project_memory_sources = []

            for path in project_paths:
                # Read project memory from sandbox if available, local otherwise
                content = self._read_file_sandbox(path)
                if content is not None and content.strip():
                    # Add header showing the source file
                    relative_path = (
                        path.relative_to(self.project_root) if self.project_root else path.name
                    )
                    combined_memories.append(f"<!-- Source: {relative_path} -->\n{content}")
                    self.loaded_project_memory_sources.append(str(relative_path))

            if combined_memories:
                result["project_memory"] = "\n\n---\n\n".join(combined_memories)

        # Record modification times after successful load (local only)
        if self._backend is None:
            self._record_mtimes(all_paths)

        return result

    async def _aread_file(self, path: Path) -> str | None:
        """Async read: through the backend via ``aread``, else local filesystem.

        Unifies :meth:`_read_file` and :meth:`_read_file_sandbox` for the async
        path — both reduce to "backend aread by virtual path, else local read".
        """
        if self._backend is not None:
            virtual_path = self._real_to_virtual(path)
            if virtual_path:
                try:
                    from novacode_cli.utils.backend_paths import aread_via_backend

                    content = await aread_via_backend(virtual_path, self._backend)
                    if content is not None:
                        return content
                except Exception:  # noqa: BLE001, S110 — best-effort; fall back to local
                    pass
        return self._read_file_local(path)

    async def abefore_agent(  # type: ignore[override]  # noqa: PLR0912 — mirrors before_agent
        self,
        state: AgentMemoryState,
    ) -> AgentMemoryStateUpdate:
        """Async load of agent memory before agent execution.

        Mirrors :meth:`before_agent`, but reads through the backend with ``aread``.
        The sync path routes sandbox reads through ``backend.read``, whose
        sync→async bridge (``run_coroutine_threadsafe``) **deadlocks** when called
        from the agent's own running loop during ``ainvoke``/``astream`` — that
        hung every sandboxed (Modal/Docker) run at
        ``AgentMemoryMiddleware.before_agent``. Async execution uses this method.
        """
        result: AgentMemoryStateUpdate = {}

        user_path = self.settings.get_user_agent_md_path(self.assistant_id)
        index_path = self.agent_dir / "memories" / "INDEX.md"
        habits_path = user_path.parent / "HABITS.md"
        project_paths = (
            self.settings.get_project_agent_md_paths() if not self.skip_project_memory else []
        )
        all_paths = [user_path, index_path, habits_path, *project_paths]
        needs_reload = self._files_changed(all_paths) if self._backend is None else True
        result.update(
            {
                key: ""
                for key in (
                    "user_memory",
                    "project_memory",
                    "memory_index",
                    "habits_memory",
                    "learning_overview",
                )
                if needs_reload
                and key in state
                and (key != "project_memory" or not self.skip_project_memory)
            }
        )

        if needs_reload or "user_memory" not in state:
            content = await self._aread_file(user_path)
            if content is not None:
                result["user_memory"] = content

        if needs_reload or "memory_index" not in state:
            index_content = await self._aread_file(index_path)
            if index_content is not None and index_content.strip():
                index_content = self._cap_index(index_content)
                result["memory_index"] = index_content

        if needs_reload or "habits_memory" not in state:
            habits_content = await self._aread_file(habits_path)
            if habits_content is not None and habits_content.strip():
                habits_content = self._cap(habits_content)
                result["habits_memory"] = habits_content

        if needs_reload or "learning_overview" not in state:
            overview = self._build_learning_overview()
            if overview:
                result["learning_overview"] = overview

        if not self.skip_project_memory and (needs_reload or "project_memory" not in state):
            combined_memories: list[str] = []
            self.loaded_project_memory_sources = []
            for path in project_paths:
                content = await self._aread_file(path)
                if content is not None and content.strip():
                    relative_path = (
                        path.relative_to(self.project_root) if self.project_root else path.name
                    )
                    combined_memories.append(f"<!-- Source: {relative_path} -->\n{content}")
                    self.loaded_project_memory_sources.append(str(relative_path))
            if combined_memories:
                result["project_memory"] = "\n\n---\n\n".join(combined_memories)

        # Record modification times after successful load (local only)
        if self._backend is None:
            self._record_mtimes(all_paths)

        return result

    @staticmethod
    def _publish_memory_notice(runtime: object, name: str, path: str, action: str) -> None:
        """Publish source metadata separately from memory bodies and model history."""
        writer = getattr(runtime, "stream_writer", None)
        if callable(writer):
            try:
                writer(
                    {
                        "type": "nova_memory_read",
                        "action": action,
                        "name": " ".join(name.split())[:200],
                        "path": path[:2000],
                    }
                )
            except Exception:  # noqa: BLE001 - Display failures must not break memory use.
                logger.debug("Could not publish memory notice", exc_info=True)

    def _announce_memory_read(self, request: ToolCallRequest, result: object) -> None:
        """Report successful explicit page reads, including guarded Code Mode calls."""
        name = request.tool_call["name"]
        if name not in {"read_file", "read_memory"}:
            return
        if not isinstance(result, ToolMessage) or result.status == "error":
            return
        if not isinstance(result.content, str) or result.content.lstrip().lower().startswith(
            "error:"
        ):
            return
        args = request.tool_call.get("args") or {}
        path = str(args.get("file_path" if name == "read_file" else "path") or "")
        if name == "read_memory":
            try:
                data = json.loads(result.content)
            except (TypeError, ValueError):
                return
            if (
                not isinstance(data, dict)
                or data.get("success") is not True
                or data.get("exists") is not True
            ):
                return
            path = str(data.get("path") or path)
        path = path.replace("\\", "/")
        if not path.endswith(".md") or not (
            path.startswith(("/memories/memories/", "/project-memory/"))
            or path in {"/memories/agent.md", "/memories/HABITS.md"}
        ):
            return
        self._publish_memory_notice(request.runtime, Path(path).stem, path, "read")

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        """Keep guards authoritative and announce only completed reads."""
        result = handler(request)
        self._announce_memory_read(request, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Async counterpart for normal and programmatic tool dispatch."""
        result = await handler(request)
        self._announce_memory_read(request, result)
        return result

    async def _memory_search_tool(
        self, query: str, runtime: ToolRuntime, k: int = 5
    ) -> str:
        """Search topic memory and rerank with the model selected for this run.

        Args:
            query: Plain words for what you need to remember — a topic, task,
                decision, or symptom.
            runtime: Current graph state, including the selected model route.
            k: Maximum number of topics to return.
        """
        model = None
        if self._model_resolver is not None and isinstance(runtime.state, Mapping):
            model = self._model_resolver(runtime.state)
        return await self._memory_search(query, k, model=model)

    async def _memory_search(
        self, query: str, k: int = 5, *, model: BaseChatModel | None = None
    ) -> str:
        """Search directly; the optional model keeps local tests provider-neutral."""
        self._memory_search_service.project_key = getattr(self, "_project_key", "")
        return await self._memory_search_service.search(query, limit=k, model=model)

    def _build_learning_overview(self) -> str:
        """Build the compact learning overview for injection.

        Aggregates memory topics, skills, prompt-evolution status, and recent
        refinement events into a single at-a-glance block (see
        ``hermes/overview.py``). Best-effort: returns ``""`` when nothing is
        available or the sources can't be read.
        """
        try:
            from novacode_cli.hermes.overview import build_learning_overview

            return build_learning_overview(
                agent_dir=self.agent_dir,
                skills_dir=self.agent_dir / "skills",
                prompt_history_dir=self.agent_dir / "prompt_history",
                refinement_log_path=self.agent_dir.parent.parent / "refinement_events.json",
            )
        except Exception:  # noqa: BLE001 — overview is best-effort, never fatal
            return ""

    def _build_system_prompt_parts(self, request: ModelRequest) -> tuple[str, str]:
        """Build the stable system prompt and an empty volatile prompt section.

        Learned topic bodies are available through ``memory_search`` and
        explicit file reads, not injected automatically on each model call.

        Returns:
            A 2-tuple of (stable_prompt, "") for the model-call middleware.
        """
        return self._build_system_prompt(request), ""

    async def _build_system_prompt_parts_async(self, request: ModelRequest) -> tuple[str, str]:
        """Async twin of :meth:`_build_system_prompt_parts`."""
        return self._build_system_prompt(request), ""

    def _build_system_prompt(self, request: ModelRequest) -> str:
        """Build the STABLE part of the system prompt (memory sections + base).

        Learned topic bodies are fetched explicitly via the memory tools, not
        included here.
        """
        import time

        # Extract memory from state
        state = cast("AgentMemoryState", request.state)
        user_memory = state.get("user_memory")
        project_memory = state.get("project_memory")
        memory_index = state.get("memory_index")
        habits_memory = state.get("habits_memory")
        learning_overview = state.get("learning_overview")
        base_system_prompt = request.system_prompt

        current_time = time.time()

        # Check if we can use cached memory section (sliding window)
        # Cache is valid if: TTL not expired AND memory content hasn't changed
        memory_content = (
            user_memory or "",
            project_memory or "",
            memory_index or "",
            habits_memory or "",
            learning_overview or "",
        )
        can_use_cache = (
            self._memory_section_cache is not None
            and current_time - self._memory_section_cache_time < self._memory_section_cache_ttl
            and (self._cached_user_memory or "") == memory_content[0]
            and (self._cached_project_memory or "") == memory_content[1]
            and (self._cached_memory_index or "") == memory_content[2]
            and (self._cached_habits_memory or "") == memory_content[3]
            and (self._cached_learning_overview or "") == memory_content[4]
        )

        if can_use_cache:
            # Sliding window: reset timer on access to keep cache alive during active use
            self._memory_section_cache_time = current_time
            memory_section = self._memory_section_cache
        else:
            # Build project memory info for documentation based on actually loaded files
            if self.project_root and self.loaded_project_memory_sources:
                sources_list = ", ".join(self.loaded_project_memory_sources)
                project_memory_info = f"`{self.project_root}` (loaded: {sources_list})"
            elif self.project_root:
                project_memory_info = (
                    f"`{self.project_root}` (no AGENTS.md, CLAUDE.md, or NOVA.md found)"
                )
            else:
                project_memory_info = "None (not in a git project)"

            # Build project deepagents directory path (virtual path)
            project_deepagents_dir = "/project-memory/"

            # Format memory section with both memories
            memory_section = self.system_prompt_template.format(
                user_memory=user_memory if user_memory else "(No user agent.md)",
                project_memory=(
                    project_memory
                    if project_memory
                    else "(No project AGENTS.md, CLAUDE.md, or NOVA.md)"
                ),
            )

            # Add longterm memory template
            memory_section += "\n\n" + render_template(
                "longterm_memory.jinja",
                agent_dir_absolute=self.agent_dir_absolute,
                agent_dir_display=self.agent_dir_display,
                project_memory_info=project_memory_info,
                project_deepagents_dir=project_deepagents_dir,
                memory_index=memory_index,
                habits_memory=habits_memory,
                learning_overview=learning_overview,
            )

            # Cache the result
            self._memory_section_cache = memory_section
            self._memory_section_cache_time = current_time
            self._cached_user_memory = memory_content[0]
            self._cached_project_memory = memory_content[1]
            self._cached_memory_index = memory_content[2]
            self._cached_habits_memory = memory_content[3]
            self._cached_learning_overview = memory_content[4]

        # memory_section is guaranteed to be set at this point
        assert memory_section is not None
        system_prompt: str = memory_section

        if base_system_prompt:
            system_prompt = base_system_prompt + "\n\n" + system_prompt

        return system_prompt

    @staticmethod
    def _make_system_message(
        request: ModelRequest, stable_prompt: str, volatile_prompt: str = ""
    ) -> "SystemMessage":
        """Build a SystemMessage, marking only the STABLE prefix as cached.

        Anthropic caches a *prefix*: the ``cache_control`` breakpoint belongs on
        the last block that is byte-identical across requests. Core instructions
        and standing memory use separate breakpoints, so a memory update can
        still reuse the core. Per-turn retrieval follows both breakpoints.

        Falls back to plain content unless the route is explicitly native Anthropic.
        """
        from langchain_core.messages import SystemMessage

        from novacode_cli.agents.provider_caching import public_provider

        if public_provider(request.model) == "anthropic":
            # Keep a reusable core breakpoint when standing memory changes.
            # Other providers benefit from the same stable leading text via
            # their automatic prefix caching, without extra request fields.
            base = request.system_prompt or ""
            parts = [stable_prompt]
            if base and stable_prompt.startswith(base + "\n\n"):
                parts = [base, stable_prompt[len(base) + 2 :]]
            content: list[Any] = [
                {
                    "type": "text",
                    "text": part,
                    "cache_control": {"type": "ephemeral"},
                }
                for part in parts
            ]
            if volatile_prompt:
                # After the breakpoint on purpose: varying content must not sit
                # inside the cached prefix, or every turn is a cache miss.
                content.append({"type": "text", "text": volatile_prompt})
            return SystemMessage(content=content)

        combined = stable_prompt + ("\n\n" + volatile_prompt if volatile_prompt else "")
        return SystemMessage(content=combined)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject agent memory into the system prompt.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        stable, volatile = self._build_system_prompt_parts(request)
        system_message = self._make_system_message(request, stable, volatile)
        return handler(request.override(system_message=system_message))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """(async) Inject agent memory into the system prompt.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        stable, volatile = await self._build_system_prompt_parts_async(request)
        system_message = self._make_system_message(request, stable, volatile)
        return await handler(request.override(system_message=system_message))
