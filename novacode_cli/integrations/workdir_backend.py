"""Workdir-rebasing wrapper for sandbox backends.

## Why this exists

Nova's agents are told (by the filesystem tool descriptions / system prompt) to
use **virtual paths starting with ``/``** that denote the *project root*. In
LOCAL mode this works because the backend is a ``FilesystemBackend`` with
``virtual_mode=True`` rooted at the project, so ``/novacode_cli/x`` →
``<project>/novacode_cli/x``.

In SANDBOX mode the project lives at the sandbox **working directory** (e.g.
``/workspace`` for modal/docker, ``/home/user`` for runloop), but the raw
sandbox backend's file ops treat a leading ``/`` as the **container root**. So
``/novacode_cli/x`` resolves to ``/novacode_cli/x`` at the container root and
404s — even though ``execute`` (shell) correctly runs in the working directory.
A LangSmith trace of ``/init`` showed 39 consecutive ``read_file`` failures from
exactly this mismatch.

``WorkdirSandboxBackend`` closes the gap: it subclasses ``BaseSandbox`` (so it
still satisfies ``SandboxBackendProtocol`` / ``isinstance`` checks and inherits
the script-building file ops), delegates the execution primitives to the wrapped
backend, and **rebases every file path onto the working directory** before the
inherited methods build their scripts. After wrapping, the agent's ``/``-rooted
virtual paths map to ``<workdir>/…`` — consistent with both the agent's mental
model and where ``execute`` actually runs.

The rebase is idempotent: a path already under the workdir is returned
unchanged, so internal re-entrant calls never double-prefix.

## Async patterns

All async file operations use ``_run_async`` which applies:

- **Timeout** (default 120s) — remote sandbox file ops can hang on
  unresponsive backends; the timeout prevents a stuck operation from
  blocking the caller indefinitely.
- **Cancellation handling** — ``asyncio.CancelledError`` is caught,
  logged, and re-raised so the caller can respond gracefully.
- **Consistent error wrapping** — transient failures (connection
  resets, timeouts) are surfaced as exceptions rather than silent
  failures.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex
from typing import TYPE_CHECKING, Any

from deepagents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)

# Default timeout for remote sandbox file operations. Operations that
# exceed this are cancelled to prevent hangs on unresponsive backends.
_DEFAULT_ASYNC_TIMEOUT: float = 120.0

# Directories that are huge and never worth grepping (dependency trees, VCS
# metadata, caches, build/output dirs). The base sandbox grep does a plain
# `grep -r` with NO exclusions, so on a real project tree it scans .venv /
# node_modules / .git etc. and blows past the timeout. We skip these in both
# the ripgrep and grep code paths to keep a project-root search fast.
_GREP_EXCLUDE_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "graphify-out",
    "graphify_out",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".next",
    "target",
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from deepagents.backends.protocol import (
        EditResult,
        ExecuteResponse,
        FileDownloadResponse,
        FileUploadResponse,
        GlobResult,
        GrepResult,
        LsResult,
        ReadResult,
        WriteResult,
    )


class WorkdirSandboxBackend(BaseSandbox):
    """Wrap a sandbox backend so ``/``-rooted virtual paths map to its workdir.

    Args:
        inner: The real sandbox backend (Docker/Modal/Daytona/Runloop, all of
            which subclass ``BaseSandbox``).
        workdir: Absolute working directory inside the sandbox where the project
            lives (e.g. ``/workspace``). Virtual ``/foo`` paths become
            ``<workdir>/foo``.
    """

    def __init__(self, inner: BaseSandbox, workdir: str) -> None:
        self._inner = inner
        # Normalise the workdir once; everything rebases against it.
        self._workdir = posixpath.normpath("/" + workdir.strip("/")) if workdir else "/"

    # ── path rebasing ────────────────────────────────────────────────────
    def _rebase(self, path: str) -> str:
        """Map a virtual/relative path onto the sandbox working directory.

        Idempotent: paths already under the workdir pass through unchanged.
        """
        if not isinstance(path, str) or not path:
            return path
        wd = self._workdir
        # Relative paths resolve under the workdir; absolute paths are treated as
        # rooted at the *project* (workdir), not the container root.
        joined = path if path.startswith("/") else posixpath.join(wd, path)
        norm = posixpath.normpath(joined)
        if norm == wd or norm.startswith(wd + "/"):
            return norm
        return posixpath.normpath(wd + "/" + norm.lstrip("/"))

    def _rebase_opt(self, path: str | None) -> str:
        """Rebase, defaulting ``None`` to the working directory (for grep/glob)."""
        return self._workdir if path is None else self._rebase(path)

    # ── abstract primitives → delegate to the wrapped backend ────────────
    @property
    def id(self) -> str:
        return self._inner.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if timeout is None:
            return self._inner.execute(command)
        return self._inner.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return await self._inner.aexecute(command, timeout=timeout)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._inner.download_files([self._rebase(p) for p in paths])

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._run_async(self._inner.adownload_files([self._rebase(p) for p in paths]))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._inner.upload_files([(self._rebase(p), b) for p, b in files])

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await self._run_async(
            self._inner.aupload_files([(self._rebase(p), b) for p, b in files])
        )

    # ── async executor helper ──────────────────────────────────────────
    async def _run_async(self, coro: Awaitable[Any]) -> Any:
        """Run an async backend operation with timeout and cancellation handling.

        Wraps every async file operation so that hangs on unresponsive remote
        backends are surfaced as ``TimeoutError``, and task cancellation
        is caught, logged, and re-raised cleanly.

        Applies: :ref:`async-python-patterns` Patterns 4 (error handling),
        5 (timeout), and 3 (task management with cancellation).

        Args:
            coro: The awaitable to run (e.g. ``super().aread(rebased, o, l)``).

        Returns:
            The result of the wrapped callable.

        Raises:
            TimeoutError: If the operation exceeds the default timeout.
            CancelledError: Propagated from the caller.
        """
        try:
            return await asyncio.wait_for(coro, timeout=_DEFAULT_ASYNC_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "Async sandbox op timed out after %ss",
                _DEFAULT_ASYNC_TIMEOUT,
            )
            raise
        except asyncio.CancelledError:
            logger.info("Async sandbox op cancelled")
            raise

    # ── file ops → rebase the path, then run the inherited implementation ──
    # super().<m>() builds the script with the rebased path and calls
    # self.execute → our delegating execute → the wrapped sandbox.
    def ls(self, path: str) -> LsResult:
        return super().ls(self._rebase(path))

    async def als(self, path: str) -> LsResult:
        return await self._run_async(super().als(self._rebase(path)))

    def ls_info(self, path: str) -> list[Any]:
        return super().ls_info(self._rebase(path))

    async def als_info(self, path: str) -> list[Any]:
        return await self._run_async(super().als_info(self._rebase(path)))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return super().read(self._rebase(file_path), offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await self._run_async(super().aread(self._rebase(file_path), offset, limit))

    def write(self, file_path: str, content: str) -> WriteResult:
        return super().write(self._rebase(file_path), content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await self._run_async(super().awrite(self._rebase(file_path), content))

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return super().edit(self._rebase(file_path), old_string, new_string, replace_all)

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return await self._run_async(
            super().aedit(self._rebase(file_path), old_string, new_string, replace_all)
        )

    @staticmethod
    def _build_grep_command(pattern: str, search_path: str, glob: str | None) -> str:
        """Build a fast, exclusion-aware content-search command.

        Prefers ``rg`` (ripgrep) — parallel, respects ``.gitignore``, and we
        additionally force-exclude the heavy directories so it stays fast even
        when the project root isn't a git repo. Falls back to ``grep -r`` with
        the same ``--exclude-dir`` set. ``; true`` keeps the shell exit status 0
        so a no-match (exit 1) isn't treated as a failure by ``execute``.
        """
        pat = shlex.quote(pattern)
        sp = shlex.quote(search_path)

        rg_excludes = " ".join(f"-g {shlex.quote('!' + d)}" for d in _GREP_EXCLUDE_DIRS)
        rg_include = f"-g {shlex.quote(glob)} " if glob else ""
        rg = f"rg -n --no-heading -F --color=never {rg_include}{rg_excludes} -e {pat} -- {sp}"

        grep_excludes = " ".join(f"--exclude-dir={shlex.quote(d)}" for d in _GREP_EXCLUDE_DIRS)
        grep_include = f"--include={shlex.quote(glob)} " if glob else ""
        gr = f"grep -rHnF {grep_excludes} {grep_include}-e {pat} {sp}"

        return (
            f"if command -v rg >/dev/null 2>&1; then {rg} 2>/dev/null; "
            f"else {gr} 2>/dev/null; fi; true"
        )

    @staticmethod
    def _parse_grep_output(output: str | None) -> list[Any]:
        """Parse ``path:line:text`` grep/ripgrep output into GrepMatch dicts."""
        matches: list[Any] = []
        for line in (output or "").rstrip().split("\n"):
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:  # noqa: PLR2004
                continue
            try:
                line_no = int(parts[1])
            except ValueError:
                continue
            matches.append({"path": parts[0], "line": line_no, "text": parts[2]})
        return matches

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        # Override the base sandbox grep (plain `grep -r`, no excludes) with a
        # fast, exclusion-aware search so a project-root grep doesn't scan
        # .venv/node_modules/.git and time out.
        from deepagents.backends.protocol import GrepResult as _GrepResult

        cmd = self._build_grep_command(pattern, self._rebase_opt(path), glob)
        try:
            result = self.execute(cmd)
        except Exception as exc:  # noqa: BLE001
            return _GrepResult(error=f"grep failed: {exc}")
        return _GrepResult(matches=self._parse_grep_output(result.output))

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> GrepResult:
        # Run our fast sync grep off-thread under the standard timeout backstop.
        from deepagents.backends.protocol import GrepResult as _GrepResult

        cmd = self._build_grep_command(pattern, self._rebase_opt(path), glob)
        try:
            result = await self._run_async(self.aexecute(cmd))
        except Exception as exc:  # noqa: BLE001
            return _GrepResult(error=f"grep failed: {exc}")
        return _GrepResult(matches=self._parse_grep_output(result.output))

    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[Any] | str:
        return super().grep_raw(pattern, self._rebase_opt(path), glob)

    async def agrep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[Any] | str:
        return await self._run_async(super().agrep_raw(pattern, self._rebase_opt(path), glob))

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        return super().glob(pattern, self._rebase(path))

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        return await self._run_async(super().aglob(pattern, self._rebase(path)))

    def glob_info(self, pattern: str, path: str = "/") -> list[Any]:
        return super().glob_info(pattern, self._rebase(path))

    async def aglob_info(self, pattern: str, path: str = "/") -> list[Any]:
        return await self._run_async(super().aglob_info(pattern, self._rebase(path)))


__all__ = ["WorkdirSandboxBackend"]
