"""Middleware for loading agent-specific long-term memory into the system prompt."""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypedDict, cast
from typing import TYPE_CHECKING

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
from novacode_cli.config.config import Settings
from novacode_cli.memory.limits import MAX_MEMORY_CHARS
from novacode_cli.prompts import render_template

# Injection-time truncation keeps the file *head* (newest, given memory files are
# written newest-first — see novacode_cli/memory/limits.py for the invariant).
_MEMORY_TRUNCATION_NOTICE = "\n\n... [memory truncated — use read_file for full content]"

# Per-turn memory retrieval tunables.
_MAX_RETRIEVED_MEMORIES = 3  # top-K topic bodies injected per turn
_RETRIEVAL_PER_FILE_CAP = 900  # chars per injected memory body
_RETRIEVAL_CHAR_BUDGET = 2200  # total chars across injected bodies
_MIN_RELEVANCE = 2  # min lexical score to inject (drops weak single-word hits)


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

    def __init__(
        self,
        *,
        settings: Settings,
        assistant_id: str,
        system_prompt_template: str | None = None,
        skip_project_memory: bool = False,
        backend: Any = None,
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
        """
        self.settings = settings
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
        self._corpus_sig: tuple[int, float] | None = None
        self._retrieval_cache: tuple[str, str] | None = None

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
                if len(content) > MAX_MEMORY_CHARS:
                    content = content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
                result["user_memory"] = content

        # Load the topic-memory index (memories/INDEX.md). It's a compact pointer
        # list, so injecting it gives the agent a live map of what it already
        # knows; it then reads the referenced topic files on demand.
        if needs_reload or "memory_index" not in state:
            index_content = self._read_file(index_path)
            if index_content is not None and index_content.strip():
                if len(index_content) > MAX_MEMORY_CHARS:
                    index_content = index_content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
                result["memory_index"] = index_content

        # Load the always-injected good-habits file (HABITS.md), if present.
        if needs_reload or "habits_memory" not in state:
            habits_content = self._read_file(habits_path)
            if habits_content is not None and habits_content.strip():
                if len(habits_content) > MAX_MEMORY_CHARS:
                    habits_content = habits_content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
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
                    if len(content) > MAX_MEMORY_CHARS:
                        content = content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
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
                if len(content) > MAX_MEMORY_CHARS:
                    content = content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
                result["user_memory"] = content

        if needs_reload or "memory_index" not in state:
            index_content = await self._aread_file(index_path)
            if index_content is not None and index_content.strip():
                if len(index_content) > MAX_MEMORY_CHARS:
                    index_content = index_content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
                result["memory_index"] = index_content

        if needs_reload or "habits_memory" not in state:
            habits_content = await self._aread_file(habits_path)
            if habits_content is not None and habits_content.strip():
                if len(habits_content) > MAX_MEMORY_CHARS:
                    habits_content = habits_content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
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
                    if len(content) > MAX_MEMORY_CHARS:
                        content = content[:MAX_MEMORY_CHARS] + _MEMORY_TRUNCATION_NOTICE
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
        signature = self._corpus_signature(mem_dir)
        if signature is None:
            self._corpus_cache, self._corpus_sig = {}, None
            return {}
        # Compare the WHOLE signature. Comparing only the newest mtime (the old
        # ``signature[1]``) discarded the file count, so deleting a topic file
        # that was not the newest left its lessons in the cached corpus — and
        # therefore injected — for the rest of the session.
        if self._corpus_cache is not None and self._corpus_sig == signature:
            return self._corpus_cache

        corpus: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {}
        for path in mem_dir.glob("*.md"):
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
        self._corpus_cache, self._corpus_sig = corpus, signature
        return corpus

    @staticmethod
    def _corpus_signature(mem_dir: Path) -> tuple[int, float] | None:
        """Return ``(file_count, newest_file_mtime)`` for the topic dir, or None.

        Keyed on the newest *file* mtime, NOT the directory's own mtime: on NTFS
        rewriting a file in place does not bump the parent directory's mtime
        (only create/delete/rename do), and the review/dream passes rewrite topic
        files in place — so a dir-mtime key served a stale corpus all session.
        """
        try:
            mtimes = [p.stat().st_mtime for p in mem_dir.glob("*.md")]
        except OSError:
            return None
        if not mtimes:
            return (0, 0.0)
        return (len(mtimes), max(mtimes))

    def _relevant_memories(self, request: ModelRequest) -> str:
        """Retrieve topic bodies relevant to this turn's user message, formatted
        for injection. Lexical overlap scoring (title hits weighted 2x); cached
        per-query so tool-loop iterations reuse the result. '' when nothing scores."""
        query = self._latest_user_text(request)
        if not query:
            return ""
        if self._retrieval_cache is not None and self._retrieval_cache[0] == query:
            return self._retrieval_cache[1]

        q = _tokens(query)
        block = ""
        if q:
            corpus = self._load_memory_corpus()
            scored = []
            for topic, (title_toks, body, body_toks) in corpus.items():
                score = 2 * len(q & title_toks) + len(q & body_toks)
                if score >= _MIN_RELEVANCE:
                    scored.append((score, topic, body))
            scored.sort(key=lambda t: t[0], reverse=True)

            used = 0
            chunks: list[str] = []
            for _score, topic, body in scored[:_MAX_RETRIEVED_MEMORIES]:
                snippet = body.strip()[:_RETRIEVAL_PER_FILE_CAP]
                if used + len(snippet) > _RETRIEVAL_CHAR_BUDGET:
                    break
                chunks.append(f"### {topic}\n{snippet}")
                used += len(snippet)
            if chunks:
                block = (
                    "<relevant_memory>\n"
                    "Lessons from past sessions relevant to this request "
                    "(retrieved automatically — apply them):\n\n"
                    + "\n\n".join(chunks)
                    + "\n</relevant_memory>"
                )
        self._retrieval_cache = (query, block)
        return block

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
        stable, volatile = self._build_system_prompt_parts(request)
        system_message = self._make_system_message(request, stable, volatile)
        return await handler(request.override(system_message=system_message))
