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


# ``BaseSandbox`` implements ls/read/write/edit/glob as **python3 scripts** run
# inside the sandbox, so every one of those tools breaks on an image without
# python3 — and ls/glob break *silently*: the bash "command not found" text
# fails their JSON parse, which they treat as an empty directory and report as a
# successful empty listing. Found on Terminal-Bench, whose task images are built
# per task: on a C and a TeX image, `read_file` failed 13 times with
# "unexpected server response: python3: command not found" and the agent fell
# back to `cat`. The probe below runs once per sandbox, and the shell fallbacks
# keep the three read-only tools working wherever there is a POSIX shell.
_PYTHON3_PROBE = "command -v python3 >/dev/null 2>&1 && echo __NOVA_PY3__"
_PY3_MARKER = "__NOVA_PY3__"
# Worded as "not installed yet", never "this sandbox has no python3": the first
# version stated it as a fact about the environment, and an agent that read it
# concluded Python was unavailable, skipped installing it, wrote its code blind
# and shipped it untested. Missing is a state the agent can change.
_NO_PYTHON3_HINT = (
    "python3 is not installed in this sandbox yet, and the file-editing tools need it. "
    "Install it with the system package manager if you can (e.g. `apt-get update && "
    "apt-get install -y python3`) — that also lets you run and test Python. Otherwise "
    "use the shell: a heredoc to write a file, `sed -i` to edit one."
)
# Bounds the fallback glob so a match-everything pattern can't flood the context.
_SHELL_GLOB_LIMIT = 1000


# Top-level paths that always mean the container's own, never the project's.
_OS_ONLY_ROOTS = ("/tmp/", "/dev/", "/proc/", "/sys/")


def _is_missing_python3(error: str | None) -> bool:
    return bool(error) and "python3" in error and "not found" in error  # type: ignore[operator]


# Nothing in a sandbox has a TTY, so any tool that stops to ask a question hangs
# until the command (or the whole turn) times out. `apt-get install python3` is
# the common one: tzdata opens debconf's "Geographic area:" menu and waits
# forever. On Terminal-Bench that burned ~10 of a task's 15 minutes before the
# agent worked out `DEBIAN_FRONTEND=noninteractive` for itself. These are the
# standard non-interactive settings every Dockerfile sets, and they only ever
# suppress a prompt that could not be answered anyway.
_NONINTERACTIVE_ENV = "export DEBIAN_FRONTEND=noninteractive TZ=${TZ:-Etc/UTC}; "


def _noninteractive(command: str) -> str:
    """Prefix a sandbox command with the env that stops installers prompting.

    Idempotent, so a re-entrant or already-prefixed command is not double-wrapped.
    """
    if not command or command.startswith(_NONINTERACTIVE_ENV):
        return command
    return _NONINTERACTIVE_ENV + command


class WorkdirSandboxBackend(BaseSandbox):
    """Wrap a sandbox backend so ``/``-rooted virtual paths map to its workdir.

    Args:
        inner: The real sandbox backend (Docker/Modal/Daytona/Runloop, all of
            which subclass ``BaseSandbox``).
        workdir: Absolute working directory inside the sandbox where the project
            lives (e.g. ``/workspace``). Virtual ``/foo`` paths become
            ``<workdir>/foo``.
    """

    def __init__(self, inner: BaseSandbox, workdir: str, *, virtual_root: bool = True) -> None:
        self._inner = inner
        # virtual_root=True (Nova's own sandboxes): the agent is told `/` means
        # the project, so `/src/x` is rebased to `<workdir>/src/x`.
        # virtual_root=False: the agent is told its REAL working directory and
        # uses real absolute paths, so only relative paths are resolved against
        # the workdir. Rebasing real paths sent `write_file("/git/server/hooks/
        # post-receive")` to `/app/git/server/hooks/post-receive` while reporting
        # success at the path asked for — the hook was never where git looked.
        self._virtual_root = virtual_root
        # Normalise the workdir once; everything rebases against it.
        self._workdir = posixpath.normpath("/" + workdir.strip("/")) if workdir else "/"
        # None until probed; see _PYTHON3_PROBE.
        self._has_python3: bool | None = None

    # ── python3 availability (probed once, then cached) ───────────────────
    def _python3(self) -> bool:
        if self._has_python3 is None:
            try:
                out = self.execute(_PYTHON3_PROBE).output or ""
            except Exception:  # noqa: BLE001 — a failed probe must not fail the op
                return True  # unknown: take the python path, which errors clearly
            self._has_python3 = _PY3_MARKER in out
        return self._has_python3

    async def _apython3(self) -> bool:
        if self._has_python3 is None:
            try:
                result = await self.aexecute(_PYTHON3_PROBE)
            except Exception:  # noqa: BLE001
                return True
            self._has_python3 = _PY3_MARKER in (result.output or "")
        return self._has_python3

    # ── path rebasing ────────────────────────────────────────────────────
    def _rebase(self, path: str) -> str:
        """Map a virtual/relative path onto the sandbox working directory.

        Idempotent: paths already under the workdir pass through unchanged.
        """
        if not isinstance(path, str) or not path:
            return path
        if not self._virtual_root and path.startswith("/"):
            return posixpath.normpath(path)  # a real absolute path: leave it be
        # Even with a virtual root, a few top-level paths can only mean the
        # operating system's: nobody keeps project code in /proc. Without this a
        # file written with `write_file("/tmp/t.py")` lands in `<workdir>/tmp/`
        # and the `python /tmp/t.py` that follows cannot find it. Deliberately a
        # short list — `/bin`, `/lib`, `/etc`, `/var` are plausible project
        # folders, so those are still rebased.
        if path.startswith(_OS_ONLY_ROOTS):
            return posixpath.normpath(path)
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
        command = _noninteractive(command)
        if timeout is None:
            return self._inner.execute(command)
        return self._inner.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return await self._inner.aexecute(_noninteractive(command), timeout=timeout)

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
        rebased = self._rebase(path)
        if not self._python3():
            return self._parse_shell_ls(self.execute(self._build_shell_ls_cmd(rebased)).output, path)
        return super().ls(rebased)

    async def als(self, path: str) -> LsResult:
        rebased = self._rebase(path)
        if not await self._apython3():
            result = await self._run_async(self.aexecute(self._build_shell_ls_cmd(rebased)))
            return self._parse_shell_ls(result.output, path)
        return await self._run_async(super().als(rebased))

    def ls_info(self, path: str) -> list[Any]:
        return super().ls_info(self._rebase(path))

    async def als_info(self, path: str) -> list[Any]:
        return await self._run_async(super().als_info(self._rebase(path)))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        rebased = self._rebase(file_path)
        if not self._python3():
            cmd = self._build_shell_read_cmd(rebased, offset, limit)
            if cmd is None:
                return self._no_lines_result()
            return self._parse_shell_read(self.execute(cmd).output, file_path, offset)
        return super().read(rebased, offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        rebased = self._rebase(file_path)
        if not await self._apython3():
            cmd = self._build_shell_read_cmd(rebased, offset, limit)
            if cmd is None:
                return self._no_lines_result()
            result = await self._run_async(self.aexecute(cmd))
            return self._parse_shell_read(result.output, file_path, offset)
        return await self._run_async(super().aread(rebased, offset, limit))

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._explain_missing_python3(super().write(self._rebase(file_path), content))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self._explain_missing_python3(
            await self._run_async(super().awrite(self._rebase(file_path), content))
        )

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return self._explain_missing_python3(
            super().edit(self._rebase(file_path), old_string, new_string, replace_all)
        )

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return self._explain_missing_python3(
            await self._run_async(
                super().aedit(self._rebase(file_path), old_string, new_string, replace_all)
            )
        )

    # ── python3-free fallbacks ───────────────────────────────────────────
    @staticmethod
    def _explain_missing_python3(result: Any) -> Any:
        """Turn a raw "python3: command not found" into something actionable.

        Write and edit have no shell fallback (replacing an exact string in
        place, with occurrence counting, is what the python script is for), but
        the agent can still do the job with a heredoc or ``sed``. It can only
        choose that if the error says so.
        """
        if _is_missing_python3(getattr(result, "error", None)):
            return type(result)(error=f"{result.error} — {_NO_PYTHON3_HINT}")
        return result

    @staticmethod
    def _no_lines_result() -> ReadResult:
        from deepagents.backends.protocol import ReadResult as _ReadResult

        return _ReadResult(
            file_data={"content": "", "encoding": "utf-8"}, no_lines_requested=True
        )

    @staticmethod
    def _build_shell_ls_cmd(path: str) -> str:
        """List a directory with one ``d|`` / ``f|`` prefixed absolute path per line."""
        p = shlex.quote(path)
        return (
            f"if [ ! -d {p} ]; then echo __NOVA_NOTDIR__; exit 0; fi; "
            f"for e in {p}/* {p}/.*; do "
            f'b=${{e##*/}}; '
            f'if [ "$b" != "." ] && [ "$b" != ".." ] && [ -e "$e" ]; then '
            f'if [ -d "$e" ]; then echo "d|$e"; else echo "f|$e"; fi; fi; '
            f"done"
        )

    @staticmethod
    def _parse_shell_ls(output: str | None, path: str) -> LsResult:
        from deepagents.backends.protocol import LsResult as _LsResult

        text = output or ""
        if "__NOVA_NOTDIR__" in text:
            return _LsResult(error=f"Path '{path}': not_a_directory")
        entries: list[Any] = []
        for line in text.split("\n"):
            line = line.strip()
            if line[1:2] != "|":
                continue
            entries.append({"path": line[2:], "is_dir": line[0] == "d"})
        return _LsResult(entries=entries)

    @staticmethod
    def _build_shell_read_cmd(path: str, offset: int, limit: int) -> str | None:
        """A ``sed``-paginated read. ``None`` when zero lines were requested."""
        if limit <= 0:
            return None
        p = shlex.quote(path)
        start = max(offset, 0) + 1
        end = start + limit - 1
        return (
            f"if [ ! -f {p} ]; then echo __NOVA_MISSING__; exit 0; fi; "
            f"if [ ! -s {p} ]; then echo __NOVA_EMPTY__; exit 0; fi; "
            # grep -I reports no match for a binary file; an empty file is
            # already handled above, so a no-match here means binary.
            f"if ! grep -qI . {p} 2>/dev/null; then echo __NOVA_BINARY__; exit 0; fi; "
            f"echo __NOVA_TOTAL__$(wc -l < {p}); "
            f"sed -n '{start},{end}p' {p}"
        )

    @staticmethod
    def _parse_shell_read(output: str | None, file_path: str, offset: int) -> ReadResult:
        from deepagents.backends.protocol import ReadResult as _ReadResult

        text = output or ""
        head = text.split("\n", 1)[0].strip()
        if head == "__NOVA_MISSING__":
            return _ReadResult(error=f"File '{file_path}' not found")
        if head == "__NOVA_EMPTY__":
            return _ReadResult(file_data={"content": "", "encoding": "utf-8"})
        if head == "__NOVA_BINARY__":
            return _ReadResult(
                error=f"File '{file_path}' is binary; read it with the shell (e.g. `base64`)."
            )
        if not head.startswith("__NOVA_TOTAL__"):
            return _ReadResult(error=f"File '{file_path}': unreadable: {text[:200]}")
        try:
            # `wc -l` counts newlines, so a file with no trailing newline reads
            # one short; the max() against the window below corrects it.
            total = int(head[len("__NOVA_TOTAL__") :].strip() or 0)
        except ValueError:
            total = 0
        body = text.split("\n")[1:]
        if body and body[-1] == "":
            body.pop()  # the trailing newline, not a source line
        if not body:  # offset past the end of the file
            return _ReadResult(file_data={"content": "", "encoding": "utf-8"})
        start_line = max(offset, 0) + 1
        end_line = start_line + len(body) - 1
        return _ReadResult(
            file_data={"content": "\n".join(body), "encoding": "utf-8"},
            total_lines=max(total, end_line),
            start_line=start_line,
            end_line=end_line,
            next_offset=end_line,
        )

    @staticmethod
    def _build_shell_glob_cmd(pattern: str, path: str) -> str:
        """``find``-based glob. ``**/`` is implicit — find already recurses."""
        pat = pattern.replace("**/", "")
        if "/" in pat:
            expr = f"-path {shlex.quote('*/' + pat.lstrip('/'))}"
        else:
            expr = f"-name {shlex.quote(pat)}"
        return f"find {shlex.quote(path)} {expr} 2>/dev/null | head -{_SHELL_GLOB_LIMIT}"

    @staticmethod
    def _parse_shell_glob(output: str | None) -> GlobResult:
        from deepagents.backends.protocol import GlobResult as _GlobResult

        matches = [
            {"path": line.strip()} for line in (output or "").split("\n") if line.strip()
        ]
        truncated = len(matches) >= _SHELL_GLOB_LIMIT
        return _GlobResult(
            matches=matches,
            truncated=truncated,
            truncation_reason="result limit" if truncated else None,
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
        rebased = self._rebase(path)
        if not self._python3():
            cmd = self._build_shell_glob_cmd(pattern, rebased)
            return self._parse_shell_glob(self.execute(cmd).output)
        return super().glob(pattern, rebased)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        rebased = self._rebase(path)
        if not await self._apython3():
            cmd = self._build_shell_glob_cmd(pattern, rebased)
            result = await self._run_async(self.aexecute(cmd))
            return self._parse_shell_glob(result.output)
        return await self._run_async(super().aglob(pattern, rebased))

    def glob_info(self, pattern: str, path: str = "/") -> list[Any]:
        return super().glob_info(pattern, self._rebase(path))

    async def aglob_info(self, pattern: str, path: str = "/") -> list[Any]:
        return await self._run_async(super().aglob_info(pattern, self._rebase(path)))


__all__ = ["WorkdirSandboxBackend"]
