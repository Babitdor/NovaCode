"""Middleware for loading agent-specific long-term memory into the system prompt."""

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
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
)

# from langgraph.runtime import Runtime
from langchain_core.tools import StructuredTool

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
_INDEX_TRUNCATION_NOTICE = (
    "\n\n... [index truncated — `memory_search` finds the rest]"
)

# Per-turn memory retrieval tunables.
_MAX_RETRIEVED_MEMORIES = 3  # top-K topic bodies injected per turn
_RETRIEVAL_PER_FILE_CAP = 900  # chars per injected memory body
_RETRIEVAL_CHAR_BUDGET = 2200  # total chars across injected bodies
_MIN_RELEVANCE = 2  # min lexical score to inject (drops weak single-word hits)
# Retrieval also looks at what the agent is facing right now — the tail of its
# last tool result — not only at what the user asked. A lesson like "`apt-get
# install` fails until `apt-get update` has run" shares no words with a request
# to build a web server; it shares several with the error it is about. Tool
# output is wordier and noisier than a request, so it has to match harder.
_SITUATION_TAIL_CHARS = 600
_MIN_SITUATION_RELEVANCE = 3
# ...except when that output is a failure. An error is short ("python3: command
# not found"), is the moment a lesson is most useful, and is far less wordy than
# a file listing, so two shared words are allowed to count there.
_MIN_FAILURE_RELEVANCE = 2
_FAILURE_MARKERS = (
    "error", "not found", "no such", "failed", "cannot", "unable", "denied",
    "traceback", "exception", "refused", "exit code: 1", "exit code: 2",
    "exit code: 100", "exit code: 126", "exit code: 127",
)  # fmt: skip
_MAX_RETRIEVED_BULLETS = 8
# How far below the best-matching lesson another may be and still be recalled.
_MEANING_MARGIN = 0.12


# Generic words (>=4 chars) that survive the length filter but carry no topical
# signal — they cause unrelated queries to spuriously match a lesson.
_STOPWORDS = frozenset(
    {
        "about", "above", "after", "again", "against", "already", "also", "another",
        "because", "been", "being", "could", "does", "doing", "done", "each", "else",
        "even", "ever", "from", "have", "here", "into", "just", "like", "made", "make",
        "many", "more", "most", "much", "need", "only", "other", "over", "please",
        "question", "really", "should", "some", "such", "than", "that", "their", "them",
        "then", "there", "these", "they", "thing", "things", "this", "those", "unrelated",
        "very", "want", "well", "were", "what", "when", "where", "which", "while", "will",
        "with", "would", "your", "yours",
    }
)


def _tokens(text: str) -> frozenset[str]:
    """Lowercased alphanumeric words of length >= 4, minus generic stopwords —
    the topical signal used for lexical relevance scoring."""
    return frozenset(
        w
        for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 4 and w not in _STOPWORDS
    )


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

        # Per-turn memory RETRIEVAL: the INDEX only injects topic pointers, so
        # learned lesson bodies never reach the model unless it chooses to read
        # them. These cache the scored topic corpus (keyed on the full file
        # signature) and the last query's retrieved block (so tool-loop
        # iterations don't re-scan).
        self._corpus_cache: dict[str, tuple[frozenset[str], str, frozenset[str]]] | None = None
        self._corpus_sig: tuple | None = None  # one (count, mtime) per lesson dir
        self._retrieval_cache: tuple[str, str] | None = None

        # Progressive disclosure for topic memory: the system prompt carries only
        # a small slice of INDEX.md, and this tool finds the rest (and the topic
        # bodies) on demand. Registered like ``skills_search``.
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._memory_search,
                name="memory_search",
                description=(
                    "Search your long-term topic memory (cross-session decisions, "
                    "facts and lessons) by what you are trying to recall. Returns "
                    "the best-matching topics with the `/memories/memories/<topic>.md` "
                    "path to read for full detail."
                ),
            )
        ]

    def _get_file_mtime(self, path: Path) -> float | None:
        """Get modification time for a file, or None if it doesn't exist."""
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _files_changed(self, paths: list[Path]) -> bool:
        """Check if any files have been modified since last load."""
        for path in paths:
            current_mtime = self._get_file_mtime(path)
            if current_mtime is not None:
                last_mtime = self._last_mtimes.get(str(path))
                if last_mtime is None or current_mtime > last_mtime:
                    return True
        return False

    def _record_mtimes(self, paths: list[Path]) -> None:
        """Record modification times for all paths."""
        for path in paths:
            mtime = self._get_file_mtime(path)
            if mtime is not None:
                self._last_mtimes[str(path)] = mtime

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

        # Load user memory if not in state or if file changed
        # User memory is read through the backend when available (virtual path: /memories/agent.md),
        # or from local filesystem as fallback.
        if needs_reload or "user_memory" not in state:
            content = self._read_file(user_path)
            if content is not None:
                content = self._cap(content)
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
                if content is not None:
                    content = self._cap(content)
                    if content.strip():
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

        if needs_reload or "user_memory" not in state:
            content = await self._aread_file(user_path)
            if content is not None:
                content = self._cap(content)
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
                if content is not None:
                    content = self._cap(content)
                    if content.strip():
                        relative_path = (
                            path.relative_to(self.project_root) if self.project_root else path.name
                        )
                        combined_memories.append(f"<!-- Source: {relative_path} -->\n{content}")
                        self.loaded_project_memory_sources.append(str(relative_path))
            if combined_memories:
                result["project_memory"] = "\n\n---\n\n".join(combined_memories)

        if self._backend is None:
            self._record_mtimes(all_paths)

        return result

    @staticmethod
    def _latest_user_text(request: ModelRequest) -> str:
        """Text of the most recent user/human message in the request, or ''."""
        messages = getattr(request, "messages", None) or []
        for msg in reversed(messages):
            role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
            if role not in ("human", "user"):
                continue
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # provider block format
                parts = [
                    b.get("text", "") for b in content if isinstance(b, dict)
                ]
                return " ".join(p for p in parts if p)
            return ""
        return ""

    def _load_memory_corpus(
        self,
    ) -> dict[str, tuple[frozenset[str], str, frozenset[str]]]:
        """Load + tokenize the topic memory files (topic -> (title_toks, body, body_toks)).

        Cached in memory, refreshed only when the memories dir changes (reviews
        add/update files), so scoring doesn't re-read 58 files every turn.
        """
        mem_dir = self.agent_dir / "memories"
        # General lessons, plus the ones recorded for THIS project. Other
        # projects' lessons sit in sibling directories and are never read.
        dirs = [mem_dir]
        project_key = getattr(self, "_project_key", "")  # absent on bare test instances
        if project_key:
            dirs.append(mem_dir / "projects" / project_key)
        signatures = [self._corpus_signature(d) for d in dirs]
        signature = tuple(signatures)
        # Compare the WHOLE signature. Comparing only the newest mtime (the old
        # ``signature[1]``) discarded the file count, so deleting a topic file
        # that was not the newest left its lessons in the cached corpus — and
        # therefore injected — for the rest of the session.
        if self._corpus_cache is not None and self._corpus_sig == signature:
            return self._corpus_cache

        corpus: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {}
        for path in (p for d in dirs if d.is_dir() for p in d.glob("*.md")):
            if path.name == "INDEX.md":
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not body.strip():
                continue
            title_toks = _tokens(path.stem.replace("-", " ").replace("_", " "))
            corpus[path.stem] = (title_toks, body, _tokens(body))
        # The lessons every installation starts with (see builtin_lessons).
        # Recalled like any other; a learned topic of the same name wins.
        from novacode_cli.memory import builtin_lessons

        if builtin_lessons.enabled() and builtin_lessons.TOPIC not in corpus:
            text = builtin_lessons.body()
            corpus[builtin_lessons.TOPIC] = (frozenset(), text, _tokens(text))
        self._corpus_cache, self._corpus_sig = corpus, signature
        return corpus

    @staticmethod
    def _corpus_signature(mem_dir: Path) -> tuple[int, float] | None:
        """Return ``(file_count, newest_file_mtime)`` for the topic dir, or None.

        Keyed on the newest *file* mtime, NOT the directory's own mtime: on NTFS
        rewriting a file in place does not bump the parent directory's mtime
        (only create/delete/rename do), and the review/dream passes rewrite topic
        files in place — so a dir-mtime key served a stale corpus all session.

        Uses ``os.scandir`` rather than ``Path.glob`` + ``stat``: this runs on
        every turn (the corpus cache is keyed on it), and each ``Path.stat`` is a
        separate syscall. On a large memory dir (18k+ topic files) that was ~600ms
        of event-loop block *per turn*; ``scandir`` returns the same count and the
        same newest mtime from the cached directory enumeration in ~40ms (14x).
        The value is identical, so the delete/rewrite invalidation guarantees are
        unchanged.
        """
        try:
            count = 0
            newest = 0.0
            with os.scandir(mem_dir) as entries:
                for entry in entries:
                    if not entry.name.endswith(".md"):
                        continue
                    count += 1
                    newest = max(newest, entry.stat().st_mtime)
        except OSError:
            return None
        if count == 0:
            return (0, 0.0)
        return (count, newest)

    async def _relevant_memories_async(self, request: ModelRequest) -> str:
        """Async wrapper around :meth:`_relevant_memories`.

        The corpus load globs and reads every topic file, which is synchronous
        disk I/O. Called from ``awrap_model_call`` on the shared UI/agent event
        loop, it froze the whole TUI: the stall watchdog recorded 70 freezes
        totalling ~2,071s with the loop blocked in ``pathlib.read_text`` /
        ``pathlib.glob`` under this call. Off-loading to a worker thread keeps
        the loop free to paint.
        """
        return await asyncio.to_thread(self._relevant_memories, request)

    def _relevant_memories(self, request: ModelRequest) -> str:
        """Retrieve the lessons relevant to this turn, formatted for injection.

        Scored per BULLET, not per topic file: a topic collects many facts over
        time and only some bear on the moment, and injecting the head of the
        file (as this used to) sent whichever bullets happened to be newest.
        A bullet qualifies by sharing words with the user's request (a hit on
        the topic's title counts double) or, more strictly, with the tail of the
        last tool result. Cached per (request, situation), so repeated model
        calls on an unchanged conversation reuse it. '' when nothing scores.
        """
        query = self._latest_user_text(request)
        situation = self._situation_text(request)
        if not query and not situation:
            return ""
        key = f"{query}\x00{situation}"
        if self._retrieval_cache is not None and self._retrieval_cache[0] == key:
            return self._retrieval_cache[1]

        q, facing_words = _tokens(query), _tokens(situation)
        lowered = situation.lower()
        needed = (
            _MIN_FAILURE_RELEVANCE
            if any(marker in lowered for marker in _FAILURE_MARKERS)
            else _MIN_SITUATION_RELEVANCE
        )
        block = ""
        if q or facing_words:
            scored: list[tuple[int, str, str]] = []
            # (word score, topic, bullet, qualified by shared words)
            candidates: list[tuple[int, str, str, bool]] = []
            for topic, (title_toks, body, body_toks) in self._load_memory_corpus().items():
                title_bonus = 2 * len(q & title_toks)
                topic_asked = title_bonus + len(q & body_toks) >= _MIN_RELEVANCE
                lines = [ln for ln in body.splitlines() if ln.lstrip().startswith(("-", "*", "•"))]
                if not lines and topic_asked:  # a hand-written topic with no bullets
                    scored.append((title_bonus + len(q & body_toks), topic, body.strip()[:_RETRIEVAL_PER_FILE_CAP]))
                for line in lines:
                    words = _tokens(line)
                    asked, facing = len(q & words), len(facing_words & words)
                    by_words = (topic_asked and asked > 0) or facing >= needed
                    candidates.append((asked + title_bonus + 2 * facing, topic, line.strip(), by_words))
            scored += self._by_meaning(candidates, query, situation)
            scored.sort(key=lambda t: t[0], reverse=True)

            used = 0
            by_topic: dict[str, list[str]] = {}
            for _score, topic, line in scored[:_MAX_RETRIEVED_BULLETS]:
                if used + len(line) > _RETRIEVAL_CHAR_BUDGET:
                    break
                by_topic.setdefault(topic, []).append(line)
                used += len(line)
            chunks = [f"### {topic}\n" + "\n".join(found) for topic, found in by_topic.items()]
            self._track_recall([ln for found in by_topic.values() for ln in found], situation)
            if chunks:
                block = (
                    "<relevant_memory>\n"
                    "Lessons from past sessions relevant to this request "
                    "(retrieved automatically — apply them):\n\n"
                    + "\n\n".join(chunks)
                    + "\n</relevant_memory>"
                )
        self._retrieval_cache = (key, block)
        return block

    def _track_recall(self, recalled: list[str], situation: str) -> None:
        """Count what was recalled, and credit the last recall if its failure cleared.

        Runs once per new (request, situation), never on a cache hit. Never raises:
        statistics must not be able to break a turn.
        """
        try:
            from novacode_cli.memory import recall_stats

            lowered = situation.lower()
            failing = any(marker in lowered for marker in _FAILURE_MARKERS)
            previous = getattr(self, "_last_recall", None)
            if previous and previous[1] and situation and not failing:
                recall_stats.note_resolved(self.agent_dir, previous[0])
            keys = recall_stats.note_recalled(self.agent_dir, recalled)
            if recalled:
                self._last_recall = (keys, failing)
            elif situation:
                self._last_recall = None  # a later success is not this lesson's doing
        except Exception:  # noqa: BLE001
            logger.debug("recall tracking failed", exc_info=True)

    @staticmethod
    def _by_meaning(
        candidates: list[tuple[int, str, str, bool]], query: str, situation: str
    ) -> list[tuple[int, str, str]]:
        """Keep the bullets that bear on this moment, judged by meaning too.

        Shared words find a lesson only when it happens to use the same ones as
        the request or the error. The embedding connects ``python3: command not
        found`` to a lesson that says "install Python with apt-get" (0.50) and
        keeps a file listing or a passing test run well away from everything
        (under 0.2). When the model is unavailable this reduces to the word
        test, which is what ran before.
        """
        if not candidates:
            return []
        from novacode_cli.memory import semantic

        bullets = [c[2].lstrip("-*• ").strip() for c in candidates]
        zeros = [0.0] * len(bullets)
        # Tool output is compared by meaning only when it is a FAILURE. Ordinary
        # output about the same tool reads as related (`python3 --version`
        # scores 0.47 against the missing-Python lesson) and is not a moment
        # anyone needs a lesson.
        lowered = situation.lower()
        failing = any(marker in lowered for marker in _FAILURE_MARKERS)
        # An error is usually the last thing printed; the whole tail of a long
        # log dilutes it, so both are tried.
        last_lines = "\n".join([ln for ln in situation.splitlines() if ln.strip()][-3:])
        closeness = [
            max(scores)
            for scores in zip(
                (semantic.similarities(situation, bullets) if failing else None) or zeros,
                (semantic.similarities(last_lines, bullets) if failing else None) or zeros,
                semantic.similarities(query[:1000], bullets) or zeros,
                strict=True,
            )
        ]
        # Only the lessons nearly as close as the closest one: the apt error
        # matches its own lesson at 0.79 and an unrelated pip note at 0.62.
        floor = max(semantic.RELEVANT, max(closeness) - _MEANING_MARGIN)
        return [
            (word_score + round(20 * close), topic, line)
            for (word_score, topic, line, by_words), close in zip(candidates, closeness, strict=True)
            if by_words or close >= floor
        ]

    @staticmethod
    def _situation_text(request: ModelRequest) -> str:
        """Tail of the most recent tool result — what the agent is looking at now."""
        messages = getattr(request, "messages", None) or []
        last = messages[-1] if messages else None
        role = getattr(last, "type", None) or (last.get("role") if isinstance(last, dict) else None)
        if role != "tool":
            return ""
        content = getattr(last, "content", None)
        if content is None and isinstance(last, dict):
            content = last.get("content")
        if isinstance(content, list):  # provider block format
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        return str(content or "")[-_SITUATION_TAIL_CHARS:]

    async def _memory_search(self, query: str, k: int = 5) -> str:
        """Search topic memory by what you are trying to recall.

        Args:
            query: Plain words for what you need to remember — a topic, task,
                decision, or symptom.
            k: Maximum number of topics to return.
        """
        # Reading + tokenizing every topic file is synchronous disk I/O; off the
        # event loop so a large memory dir cannot stall the TUI.
        corpus = await asyncio.to_thread(self._load_memory_corpus)
        # The built-in lessons have no file behind them to point the agent at;
        # they are recalled automatically, so the search covers learned topics.
        from novacode_cli.memory import builtin_lessons

        corpus = {k: v for k, v in corpus.items() if k != builtin_lessons.TOPIC}
        if not corpus:
            return "No topic memory yet. Proceed without it."
        q = _tokens(query)
        scored: list[tuple[int, str, str]] = []
        if q:
            for topic, (title_toks, body, body_toks) in corpus.items():
                score = 2 * len(q & title_toks) + len(q & body_toks)
                if score >= _MIN_RELEVANCE:
                    scored.append((score, topic, body))
        # An exact topic name always wins, even on a weak lexical score.
        wanted = query.strip().lower().replace(" ", "-").replace("_", "-")
        if wanted in corpus and not any(t == wanted for _, t, _ in scored):
            scored.append((999, wanted, corpus[wanted][1]))
        if not scored:
            return f"No memory matches {query!r}. Proceed without it."
        scored.sort(key=lambda t: (-t[0], t[1]))
        lines = [
            f"- **{topic}** `/memories/memories/{topic}.md`: "
            f"{' '.join(body.strip().split())[:240]}"
            for _score, topic, body in scored[: max(1, min(k, 20))]
        ]
        return "\n".join(lines)

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
        """Build the system prompt as (stable, volatile) parts.

        The split is the whole point: Anthropic's cache hierarchy is
        ``tools → system → messages`` and a change at any level invalidates that
        level and everything after it. Nova's per-turn memory RETRIEVAL block
        varies with the user's exact wording, so if it sits *ahead* of the base
        system prompt every distinctly-worded turn diverges the prefix and the
        entire base prompt is re-billed at full input price — the cache never hits.

        Keeping the volatile block last lets ``_make_system_message`` place the
        breakpoint on the stable tail, so the memory section + base prompt stay
        cached across turns regardless of how the user phrases a request.

        Returns:
            2-tuple of (stable_prompt, volatile_prompt). ``volatile_prompt`` is
            "" when no lessons were retrieved for this turn.
        """
        stable = self._build_system_prompt(request)
        volatile = self._relevant_memories(request)
        return stable, volatile

    async def _build_system_prompt_parts_async(self, request: ModelRequest) -> tuple[str, str]:
        """Async twin of :meth:`_build_system_prompt_parts`.

        Identical result, but the retrieval half (which reads the topic corpus
        off disk) runs in a worker thread so it cannot stall the event loop.
        """
        stable = self._build_system_prompt(request)
        volatile = await self._relevant_memories_async(request)
        return stable, volatile

    def _build_system_prompt(self, request: ModelRequest) -> str:
        """Build the STABLE part of the system prompt (memory sections + base).

        Deliberately excludes the per-turn retrieval block so the result is
        stable across turns and therefore cacheable — see
        :meth:`_build_system_prompt_parts`, which is what the model-call
        wrappers use.
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
                project_memory_info = f"`{self.project_root}` (no CLAUDE.md or NOVA.md found)"
            else:
                project_memory_info = "None (not in a git project)"

            # Build project deepagents directory path (virtual path)
            project_deepagents_dir = "/project-memory/"

            # Format memory section with both memories
            memory_section = self.system_prompt_template.format(
                user_memory=user_memory if user_memory else "(No user agent.md)",
                project_memory=(
                    project_memory if project_memory else "(No project CLAUDE.md or NOVA.md)"
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

        # NOTE: the per-turn retrieval block is deliberately NOT appended here.
        # It varies with the user's wording, so including it would put volatile
        # content ahead of the base system prompt and defeat the prompt cache on
        # every distinctly-worded turn. _build_system_prompt_parts adds it as a
        # separate trailing block, after the cache breakpoint.

        if base_system_prompt:
            system_prompt += "\n\n" + base_system_prompt

        return system_prompt

    @staticmethod
    def _make_system_message(
        request: ModelRequest, stable_prompt: str, volatile_prompt: str = ""
    ) -> "SystemMessage":
        """Build a SystemMessage, marking only the STABLE prefix as cached.

        Anthropic caches a *prefix*: the ``cache_control`` breakpoint belongs on
        the last block that is byte-identical across requests. The stable block
        (memory sections + base system prompt) qualifies; the per-turn retrieval
        block does not, so it is emitted after the breakpoint and never
        invalidates the cached prefix.

        Falls back to a plain concatenated SystemMessage for non-Anthropic
        models. Uses module-name inspection instead of an isinstance check to
        avoid importing langchain_anthropic on every call (and to stay safe when
        the package is absent or partially installed).
        """
        from langchain_core.messages import SystemMessage

        model_module = type(request.model).__module__.lower()
        if "anthropic" in model_module:
            content: list[Any] = [
                {
                    "type": "text",
                    "text": stable_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
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
