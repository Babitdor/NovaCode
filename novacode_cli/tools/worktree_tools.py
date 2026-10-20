"""Session-owned worktree transitions; never change the process working directory."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import tool
from langchain_core.messages import SystemMessage, ToolMessage

from novacode_cli.sessions import worktree as wt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from deepagents.backends.composite import CompositeBackend
    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest

WORKTREE_TOOL_NAMES = frozenset({"EnterWorktreeTool", "ExitWorktreeTool"})


class WorktreeSession:
    """Manage one graph's active workspace."""

    def __init__(self, root: Path, switch: Callable[[Path], None]) -> None:
        """Keep workspace state private to the graph that owns the tools."""
        self.root = root.resolve()
        self.switch = switch
        self.original: Path | None = None
        self.info: wt.WorktreeInfo | None = None

    def enter(self, name: str) -> dict[str, Any]:  # noqa: PLR0911 - Fail before changing roots.
        """Create a fresh branch and switch only after creation succeeds."""
        if self.original is not None:
            return {"error": "Already in a tool-managed worktree; exit it first."}
        if not name.strip():
            return {"error": "A nonempty worktree name is required."}
        repo = wt.repo_root(self.root)
        if repo is None or not wt.has_commits(repo):
            return {"error": "Entering a worktree requires a Git repository with a commit."}
        # Never reuse another session's named branch or directory.
        unique_name = f"{wt.slugify(name, 'work')[:24]}-{uuid4().hex[:12]}"
        try:
            info = wt.create_worktree(unique_name, repo=repo)
            if not info.created or info.branch is None:
                return {"error": "Could not create an isolated worktree."}
            destination = info.path / self.root.relative_to(repo.resolve())
            if not destination.is_dir():
                return {"error": f"Subdirectory is absent in HEAD. Worktree kept at {info.path}"}
            self.switch(destination)
        except (OSError, RuntimeError, ValueError) as exc:
            return {"error": str(exc)}
        self.original, self.root, self.info = self.root, destination, info
        return {"path": str(destination), "branch": info.branch, "warnings": info.warnings}

    def exit(self) -> dict[str, Any]:
        """Restore the original workspace, retaining all files and commits."""
        if self.original is None or self.info is None:
            return {"error": "This session has not entered a tool-managed worktree."}
        if not self.original.is_dir():
            return {"error": "Original workspace is unavailable; staying in the worktree."}
        try:
            self.switch(self.original)
        except (OSError, RuntimeError, ValueError) as exc:
            return {"error": str(exc)}
        result = {
            "path": str(self.original),
            "kept_worktree": str(self.info.path),
            "branch": self.info.branch,
        }
        self.root, self.original, self.info = self.original, None, None
        return result


class WorktreeToolsMiddleware(AgentMiddleware):
    """Register workspace tools and exclude transitions from parallel tool work."""

    def __init__(
        self, root: Path, backend: CompositeBackend, middleware: list[Any], *, enabled: bool = True
    ) -> None:
        """Bind roots to one local agent; remote/restricted agents fail closed."""
        super().__init__()
        self._backend = backend
        self._middleware = list(middleware)
        self._enabled = enabled
        self._lock = Lock()
        self._active = 0
        self._transition = False
        self.session = WorktreeSession(root, self._switch)

        @tool("EnterWorktreeTool")
        def enter(name: str = "work") -> dict[str, Any]:
            """Move this session into a fresh isolated Git worktree branched from HEAD.

            Call alone, without parallel tools. Uncommitted files stay in the original
            workspace. Requires a local committed repository. File and shell tools
            use the returned path. MCP servers and existing background jobs retain
            their own workspaces. Use ExitWorktreeTool to return, keeping all work.
            """
            if not self._enabled:
                return {
                    "error": "Worktree switching is unavailable in remote or restricted agents."
                }
            return self.session.enter(name)

        @tool("ExitWorktreeTool")
        def exit_worktree() -> dict[str, Any]:
            """Return this session to its original workspace, keeping the worktree and branch.

            Call alone, without parallel tools. Files and commits are preserved;
            this does not merge changes or delete the worktree.
            """
            return self.session.exit()

        self.tools = [enter, exit_worktree]

    def _switch(self, root: Path) -> None:
        """Rebase local file, shell and project-memory roots without os.chdir."""
        default = self._backend.default
        default.cwd = root
        routes = dict(self._backend.sorted_routes)
        for prefix, suffix in (("/.nova/plans/", ".nova/plans"), ("/project-memory/", ".nova")):
            if prefix in routes:
                routes[prefix].cwd = root / suffix
        for item in self._middleware:
            if type(item).__name__ == "ShellMiddleware":
                item._workspace_root = str(root)
                if getattr(item, "_os_policy", None) is not None:
                    item._os_policy = replace(item._os_policy, workspace_root=str(root))
            elif type(item).__name__ == "AgentMemoryMiddleware":
                item.project_root = root
                item._cached_project_memory = None
                item._memory_section_cache = None
                item._memory_section_cache_time = 0

    @contextmanager
    def _guard(self, request: ToolCallRequest) -> Iterator[ToolMessage | None]:
        """Reserve a transition only when it is the sole tool operation."""
        transition = request.tool_call["name"] in WORKTREE_TOOL_NAMES
        calls = (
            getattr(request.state.get("messages", [])[-1], "tool_calls", [])
            if request.state.get("messages")
            else []
        )
        with self._lock:
            denied = self._transition or (transition and (self._active > 0 or len(calls) > 1))
            if not denied:
                self._active += 1
                self._transition = transition
        if denied:
            yield ToolMessage(
                content=(
                    "Workspace switching must run alone. "
                    "Wait for other tool calls to finish and retry separately."
                ),
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
                status="error",
            )
            return
        try:
            yield None
        finally:
            with self._lock:
                self._active -= 1
                if transition:
                    self._transition = False

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[..., Any]) -> Any:  # noqa: ANN401 - Framework tool result.
        """Guard synchronous tools including calls from worker threads."""
        with self._guard(request) as error:
            return error if error is not None else handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[..., Awaitable[Any]]
    ) -> Any:  # noqa: ANN401 - Framework tool result.
        """Guard async tools without holding a thread lock across an await."""
        with self._guard(request) as error:
            if error is not None:
                return error
            if request.tool_call["name"] not in WORKTREE_TOOL_NAMES:
                return await handler(request)
            # Sync Git tools run in LangChain's thread pool. Cancellation must
            # not release the reservation while that thread can still switch roots.
            pending = asyncio.ensure_future(handler(request))
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise

    def _model_request(self, request: ModelRequest) -> ModelRequest:
        note = (
            f"\n<active_workspace>Current file and shell workspace: {self.session.root}. "
            "This supersedes workspace paths in earlier messages and shell descriptions. "
            "Call EnterWorktreeTool and ExitWorktreeTool alone.</active_workspace>"
        )
        content = list(request.system_message.content_blocks) if request.system_message else []
        return request.override(
            system_message=SystemMessage(content=[*content, {"type": "text", "text": note}])
        )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[..., ModelResponse]
    ) -> ModelResponse:
        """Keep the active root authoritative after workspace changes."""
        return handler(self._model_request(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[..., Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """Async model hook."""
        return await handler(self._model_request(request))
