"""The background dreamer: memory consolidation on its own agent and thread.

An automatic dream must not touch the user's conversation or project, and must
never stop to ask for approval while nobody is watching. So it gets its own
agent: file tools confined to the memory directory (served at ``/memories/``,
the paths the consolidation prompt uses) plus the ``memory_wiki`` tool, and no
shell, no project files, no subagents and no HITL. It runs on an InMemorySaver,
isolated from the main checkpointer, like the /btw agent.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.graph import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.agents.model_retry import NovaModelRetryMiddleware as ModelRetryMiddleware
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend
from novacode_cli.errors import is_retryable_model_error

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel
    from langgraph.pregel import Pregel

__all__ = ["BACKGROUND_NOTE", "create_dream_agent"]

_SYSTEM_PROMPT = """\
You are reviewing persistent memory as an LLM wiki. Read relevant sources and
stage concise, evidence-bearing memory proposals with propose_memory_lesson.
Never use file tools to change semantic page content; changes require explicit
user approval through /learning. Work only under /memories/.
"""

#: Appended to the consolidation prompt for a background run.
BACKGROUND_NOTE = """\
## Running in the background

- Nobody is watching: do not ask questions. Record any conflict you cannot
  resolve in your report and leave both versions in place.
- `memory_search` and `search_tools` are not available here. Find overlapping
  notes with `grep` and `glob` under `/memories/`; `memory_wiki` is bound directly.
- Stage semantic changes with `propose_memory_lesson`; do not call file write,
  edit, or delete tools. Proposals remain pending until the user approves them.
- Use project scope only for the active project's own notes; use shared scope
  only for genuinely reusable facts. Cite the source page paths as evidence.
- Convert relative dates ("yesterday", "last week") to absolute dates.
- End with a report of at most five lines.
"""


class ReadOnlyMemoryBackend(OptimizedFilesystemBackend):
    """Filesystem view for dreams: readable and searchable, never writable."""

    def __init__(self, root_dir: str, *, project_key: str = "") -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.project_key = project_key

    def _resolve_path(self, key: str) -> Path:
        from novacode_cli.integrations.host_path import host_path_to_virtual
        from novacode_cli.security.paths import virtual_relative_path

        virtual = host_path_to_virtual(key, str(self.cwd))
        lexical = self.cwd / virtual_relative_path(virtual)
        projects = self.cwd / "memories" / "projects"
        try:
            lexical_scope = lexical.relative_to(projects).parts
        except ValueError:
            lexical_scope = ()
        if lexical_scope and (not self.project_key or lexical_scope[0] != self.project_key):
            message = "Dream memory access is limited to the active project."
            raise ValueError(message)
        resolved = super()._resolve_path(key)
        root = self.cwd.resolve()
        if not resolved.is_relative_to(root):
            message = "Dream memory symlink escapes the active agent directory."
            raise ValueError(message)
        try:
            resolved_project = resolved.relative_to(projects.resolve())
        except ValueError:
            return resolved
        if resolved_project.parts and (
            not self.project_key or resolved_project.parts[0] != self.project_key
        ):
            message = "Dream project memory symlink escapes its scope."
            raise ValueError(message)
        return resolved

    def _path_allowed(self, path: str) -> bool:
        try:
            self._resolve_path(path)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def ls(self, path: str) -> LsResult:
        try:
            self._resolve_path(path)
        except ValueError as exc:
            return LsResult(error=str(exc))
        result = super().ls(path)
        if result.entries is not None:
            result.entries = [
                entry for entry in result.entries if self._path_allowed(entry["path"])
            ]
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        if path is not None:
            try:
                self._resolve_path(path)
            except ValueError as exc:
                return GlobResult(error=str(exc))
        result = super().glob(pattern, path)
        if result.matches is not None:
            result.matches = [
                match for match in result.matches if self._path_allowed(match["path"])
            ]
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        use_regex: bool = False,
    ) -> GrepResult:
        if path is not None:
            try:
                self._resolve_path(path)
            except ValueError as exc:
                return GrepResult(error=str(exc), matches=[])
        result = super().grep(pattern, path, glob, use_regex=use_regex)
        if result.matches is not None:
            result.matches = [
                match for match in result.matches if self._path_allowed(match["path"])
            ]
        return result

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            self._resolve_path(file_path)
        except ValueError as exc:
            return ReadResult(error=str(exc))
        return super().read(file_path, offset=offset, limit=limit)

    def write(self, file_path: str, content: str) -> WriteResult:  # noqa: ARG002
        return WriteResult(error="Dream memory files are read-only; stage a proposal instead.")

    def edit(
        self,
        file_path: str,  # noqa: ARG002
        old_string: str,  # noqa: ARG002
        new_string: str,  # noqa: ARG002
        replace_all: bool = False,  # noqa: ARG002, FBT001, FBT002
    ) -> EditResult:
        return EditResult(error="Dream memory files are read-only; stage a proposal instead.")

    def delete(self, file_path: str) -> DeleteResult:  # noqa: ARG002
        return DeleteResult(error="Dream memory files are read-only; stage a proposal instead.")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(
                path=path,
                error="Dream memory files are read-only; stage a proposal instead.",
            )
            for path, _content in files
        ]


def build_dream_backend(agent_dir: Path, project_key: str = "") -> CompositeBackend:
    """Build the dream agent's backend: ``/memories/`` on disk, scratch in state.

    The memory route uses Nova's backend, not upstream's: the upstream ripgrep
    path decodes search output with the platform locale codec (cp1252 on
    Windows), so a single non-ASCII character in a memory file raised
    ``UnicodeDecodeError`` and killed the whole dream.
    """
    return CompositeBackend(
        default=StateBackend(),
        routes={"/memories/": ReadOnlyMemoryBackend(str(agent_dir), project_key=project_key)},
    )


def create_dream_agent(
    model: BaseChatModel, agent_dir: Path, project_key: str | None = None
) -> Pregel:
    """Build the dream agent over ``agent_dir`` (the agent's memory root)."""
    from novacode_cli.hermes.memory_tiers import project_memory_key
    from novacode_cli.security.prompt_injection import PromptInjectionMiddleware
    from novacode_cli.tools.memory_wiki_tools import (
        create_memory_lesson_proposal_tool,
        create_memory_wiki_tool,
    )

    if project_key is None:
        from novacode_cli.config.config import Settings

        project_key = project_memory_key(Settings.from_environment().project_root)
    if project_key and not re.fullmatch(r"[a-zA-Z0-9_-]+", project_key):
        message = "Unsafe dream project scope"
        raise ValueError(message)

    agent: Any = create_deep_agent(
        name="dream-agent",
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        tools=[
            create_memory_wiki_tool(agent_dir),
            create_memory_lesson_proposal_tool(agent_dir, project_key),
        ],
        checkpointer=InMemorySaver(),
        backend=build_dream_backend(agent_dir, project_key),
        store=None,
        interrupt_on={},  # background: never pause for approval
        subagents=[],
        middleware=[
            ModelRetryMiddleware(
                max_retries=3,
                retry_on=is_retryable_model_error,
                on_failure="error",
                backoff_factor=1.5,
                initial_delay=0.5,
            ),
            PromptInjectionMiddleware(),
        ],
    )
    return agent
