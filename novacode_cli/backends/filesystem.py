"""Optimized FilesystemBackend wrapping deepagents.backends.filesystem.

Improvements over the upstream :class:`FilesystemBackend`, all delivered as
method overrides (the deepagents package is never modified — the filesystem
tools call ``backend.read/edit/glob/grep``, so overriding those backend methods
improves the tools transparently):

1. **grep hang fix** — the upstream Python fallback walks the tree with
   ``rglob("*")``, which cannot skip large, uninteresting directories (``.venv``,
   ``.git``, ``node_modules``). We override ``_python_search`` to use ``os.walk``
   with in-place ``dirnames`` pruning so those subtrees are never descended into.

2. **Regex support** — ``grep()`` gains an opt-in ``use_regex`` flag, fully
   backward-compatible with the literal default.

3. **Smart-case grep** — a lower-case pattern matches case-insensitively; a
   pattern with any upper-case letter stays case-sensitive (ripgrep's default).

4. **read encoding recovery** — ``read()`` falls back to ``utf-8-sig`` / cp1252 /
   latin-1 when a file isn't utf-8, instead of returning a decode error.

5. **edit failure hints** — a failed ``edit()`` gets an actionable hint (closest
   line for a miss, replace_all/disambiguation tip for an ambiguous match).

6. **glob prune + ordering** — ``glob()`` drops vendored-dir noise and returns
   newest-first.

Async tool calls (``aread``/``aedit``/``aglob``/``agrep``) delegate to these
sync methods via ``asyncio.to_thread`` in the protocol base, so they are covered
too without overriding the async variants.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import wcmatch.glob as wcglob
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import EditResult, GlobResult, GrepResult, ReadResult

__all__ = ["OptimizedFilesystemBackend", "OptimizedLocalShellBackend"]

# Wall-clock budget for a single grep, in seconds. Defined locally rather than
# imported from deepagents.backends.protocol: the constant only exists in
# deepagents>=0.6.8, but Nova is launched against environments still on 0.6.7
# (e.g. a miniconda install). The value matches upstream's default.
_GREP_TIMEOUT = 30


@functools.cache
def _resolve_ripgrep_path() -> str | None:
    r"""Locate the ``rg`` executable, more thoroughly than ``shutil.which``.

    The upstream resolver only checks ``PATH``. On Windows, package managers like
    winget install ripgrep as a loose archive and never link it onto ``PATH``
    (e.g. ``...\\WinGet\\Packages\\BurntSushi.ripgrep...\\rg.exe``), so the agent
    silently falls back to the slow Python search even though ripgrep is present.

    Resolution order:
    1. ``NOVA_RIPGREP_PATH`` / ``RIPGREP_PATH`` env override (explicit wins).
    2. ``shutil.which`` (honours ``PATH`` — the fast, normal case).
    3. Known package-manager install locations (winget, VS Code bundle, cargo,
       scoop, chocolatey), globbed for the versioned subdirectories they use.

    Cached for the process lifetime; ``cache_clear()`` re-probes.
    """
    for env_var in ("NOVA_RIPGREP_PATH", "RIPGREP_PATH"):
        override = os.environ.get(env_var)
        if override and Path(override).is_file():
            return override

    for name in ("rg", "rg.exe"):
        found = shutil.which(name)
        if found:
            return found

    # Glob patterns for package managers that don't touch PATH. Each may contain
    # a version directory, so we glob and take the newest match.
    local = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    programdata = os.environ.get("PROGRAMDATA", "")
    candidates: list[str] = []
    if local:
        candidates += [
            # winget loose-archive install
            rf"{local}\Microsoft\WinGet\Packages\BurntSushi.ripgrep*\**\rg.exe",
            # VS Code bundled ripgrep
            rf"{local}\Programs\Microsoft VS Code\resources\app\node_modules*\**\rg.exe",
        ]
    if userprofile:
        candidates += [
            rf"{userprofile}\.cargo\bin\rg.exe",
            rf"{userprofile}\scoop\shims\rg.exe",
            rf"{userprofile}\scoop\apps\ripgrep\**\rg.exe",
        ]
    if programdata:
        candidates.append(rf"{programdata}\chocolatey\bin\rg.exe")

    newest: tuple[float, str] | None = None
    for pattern in candidates:
        for match in _glob_abs(pattern):
            try:
                if not match.is_file():
                    continue
                mtime = match.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, str(match))
    return newest[1] if newest else None


def _glob_abs(windows_pattern: str) -> list[Path]:
    r"""Glob an absolute Windows pattern by anchoring at its drive root.

    ``Path.glob`` rejects absolute/drive-anchored patterns, so split off the
    anchor (e.g. ``C:\\``) and glob the remainder against it:
    ``C:\\a\\**\\rg.exe`` → ``Path("C:\\").glob("a/**/rg.exe")``.
    """
    p = Path(windows_pattern)
    anchor = p.anchor
    if not anchor:
        return list(Path().glob(str(p).replace("\\", "/")))
    rel = str(p)[len(anchor) :].replace("\\", "/")
    try:
        return list(Path(anchor).glob(rel))
    except (OSError, ValueError):
        return []


# Directory names pruned during the Python grep fallback. These are virtually
# always gitignored and can each hold millions of files; descending into them is
# what made the fallback hang. An explicit search *into* one of these (e.g.
# path="/node_modules") still works — only nested occurrences are skipped.
_SKIP_DIRS = frozenset(
    {
        ".venv",
        ".env",
        "venv",
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
        ".cargo",
        "target",
        "out",
        ".vscode",
        ".idea",
    }
)


# deepagents changed its internal grep contract in 0.7.0, and pyproject allows
# both (``deepagents>=0.6.8``):
#
#   0.6.10: _ripgrep_search(pattern, base, glob)             -> dict | None
#           _python_search(pattern, base, glob, *, timeout)   -> (dict, error)
#   0.7.0:  _ripgrep_search(pattern, base, glob, max_count)   -> (dict | None, truncated)
#           _python_search(..., *, max_count, timeout)        -> (dict, truncated, error)
#
# The parent's own ``grep()`` unpacks whichever shape ITS version expects, so an
# override that returns only one shape breaks the other install ("'tuple' object
# has no attribute 'items'" on 0.6.x). Detect the installed contract once and
# have the overrides return the matching shape.
_PARENT_RG_V2 = "max_count" in inspect.signature(FilesystemBackend._ripgrep_search).parameters
_PARENT_PY_V2 = "max_count" in inspect.signature(FilesystemBackend._python_search).parameters


def _cap_results(
    results: dict[str, list[tuple[int, str]]], max_count: int | None
) -> tuple[dict[str, list[tuple[int, str]]], bool]:
    """Trim *results* to at most ``max_count`` matches, reporting truncation.

    Mirrors the upstream grep contract: the caller gets exactly ``max_count``
    matches and ``truncated=True`` only when matches were actually dropped.
    """
    if max_count is None or max_count < 0:
        return results, False
    kept = 0
    capped: dict[str, list[tuple[int, str]]] = {}
    truncated = False
    for fpath, items in results.items():
        if kept >= max_count:
            truncated = True
            break
        room = max_count - kept
        capped[fpath] = items[:room]
        if len(items) > room:
            truncated = True
        kept += len(capped[fpath])
    return capped, truncated


class OptimizedFilesystemBackend(FilesystemBackend):
    """FilesystemBackend with a non-hanging grep fallback and regex support."""

    def _to_virtual_path(self, path: str | Path) -> str:
        """Map a real path to its virtual form, following symlinks/junctions.

        The parent does ``path.resolve().relative_to(self.cwd)``. When a child is
        a symlink or Windows junction whose *target* lives outside the root, that
        raises ``ValueError`` and the entry is silently dropped from ``ls``/glob —
        so a skill installed as ``~/.claude/skills/<name>`` junctioned to
        ``~/.agents/skills/<name>`` never gets discovered (``/<name>`` command
        "isn't a recognized command"). Claude Code follows these links; Nova must
        too.

        Fix: if the strict resolve escapes the root, fall back to the child's
        *lexical* position — resolve the PARENT (normal chain) and re-attach the
        child's own name. A genuine escape (the parent itself is outside root)
        still raises, preserving the containment guard.
        """
        p = Path(path)
        try:
            return super()._to_virtual_path(p)
        except ValueError:
            # relative_to raises if the parent is also outside the root → genuine
            # escape, let it propagate (excluded, as before).
            rel = p.parent.resolve().relative_to(self.cwd)
            return "/" + (rel / p.name).as_posix()

    def _resolve_path(self, key: str) -> Path:
        """Resolve an inbound path, following symlinks/junctions rooted in-tree.

        The parent rejects any path whose *resolved* target escapes the root. That
        also rejects a legitimate junction placed inside the root — e.g. a skill
        installed as ``~/.claude/skills/<name>`` that points to
        ``~/.agents/skills/<name>`` — so ``read``/``glob``/``download_files`` fail
        for it and ``/<name>`` "isn't a recognized command".

        Virtual paths already forbid ``..`` and ``~`` (below), so a virtual path
        can never climb out of the root *lexically*; the resolved target can only
        escape by following a symlink/junction that physically lives inside the
        root — which is the intended skill-install layout, and what Claude Code
        itself follows. So we keep the parent's traversal guard and the symlink-
        loop guard, and drop only the resolved-target containment rejection.
        Non-virtual mode is unchanged.
        """
        if not self.virtual_mode:
            return super()._resolve_path(key)
        from deepagents.backends.filesystem import _raise_if_symlink_loop

        vpath = key if key.startswith("/") else "/" + key
        if ".." in vpath or vpath.startswith("~"):
            msg = "Path traversal not allowed"
            raise ValueError(msg)
        full = (self.cwd / vpath.lstrip("/")).resolve()
        _raise_if_symlink_loop(full)
        return full

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        use_regex: bool = False,
    ) -> GrepResult:
        """Search for a pattern in files.

        Args:
            pattern: Text to search for. Literal by default; a regular expression
                when ``use_regex=True``.
            path: Directory or file to search in. Defaults to the current directory.
            glob: Optional glob to filter which files are searched (e.g. ``"*.py"``).
            use_regex: If ``False`` (default) ``pattern`` is matched literally; if
                ``True`` it is compiled as a Python regular expression.

        Returns:
            A ``GrepResult`` with matches or an error.
        """
        # Literal mode is the parent's exact behaviour — and since the parent
        # calls self._ripgrep_search / self._python_search, it already picks up
        # both of our overrides (hang fix included). No need to duplicate it.
        if not use_regex:
            return super().grep(pattern, path, glob)

        # Regex mode: same orchestration as the parent, but the pattern is passed
        # through unescaped to both ripgrep (no -F) and the Python fallback.
        try:
            base_full = self._resolve_path(path or ".")
            if not base_full.exists():
                return GrepResult(matches=[])
        except ValueError:
            return GrepResult(matches=[])
        except (OSError, RuntimeError) as e:
            return GrepResult(error=f"Error searching path '{path or '.'}': {e}", matches=[])

        try:
            re.compile(pattern)
        except re.error as e:
            return GrepResult(error=f"Invalid regex pattern: {e}", matches=[])

        # Call the impls, not the version-adaptive overrides: these always return
        # the rich tuple regardless of which deepagents is installed.
        results, truncated = self._rg_impl(pattern, base_full, glob, use_regex=True)
        partial_error: str | None = None
        if results is None:
            results, truncated, partial_error = self._py_impl(pattern, base_full, glob)

        matches = [
            {"path": fpath, "line": int(line_num), "text": line_text}
            for fpath, items in results.items()
            for line_num, line_text in items
        ]
        # GrepResult gained `truncated` in 0.7; 0.6.x rejects the kwarg.
        if _PARENT_RG_V2:
            return GrepResult(error=partial_error, matches=matches, truncated=truncated)
        return GrepResult(error=partial_error, matches=matches)

    def _ripgrep_search(
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        max_count: int | None = None,
        *,
        use_regex: bool = False,
    ):
        """Version-adaptive override the parent ``grep()`` calls.

        Returns ``(results, truncated)`` on deepagents >= 0.7 and a bare
        ``results`` dict on 0.6.x — matching whatever the installed parent
        unpacks. Nova's own code should call :meth:`_rg_impl` instead, which
        always returns the richer tuple.
        """
        out = self._rg_impl(pattern, base_full, include_glob, max_count, use_regex=use_regex)
        return out if _PARENT_RG_V2 else out[0]

    def _rg_impl(  # noqa: PLR0912, PLR0915
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        max_count: int | None = None,
        *,
        use_regex: bool = False,
    ) -> tuple[dict[str, list[tuple[int, str]]] | None, bool]:
        """Search with ripgrep.

        Identical to the upstream implementation except that the fixed-string
        flag (``-F``) is omitted when ``use_regex`` is set, so the pattern is
        interpreted as a regular expression. The parent's literal ``grep()`` call
        passes no ``use_regex`` and therefore keeps ``-F``.

        Returns ``(results, truncated)`` — matching the upstream contract, which
        the parent ``grep()`` unpacks. ``results`` is ``None`` to signal "fall
        back to the Python search" (ripgrep missing, timed out, or errored);
        ``truncated`` is True when ``max_count`` capped the result set.
        """
        rg_path = _resolve_ripgrep_path()
        if rg_path is None:
            return None, False

        # --smart-case: a lower-case pattern matches case-insensitively, but a
        # pattern containing an upper-case letter stays case-sensitive. Matches
        # ripgrep's CLI default and the Python fallback below, so literal grep is
        # forgiving for the common lower-case query without losing precision.
        cmd = [rg_path, "--json", "--smart-case"]
        if not use_regex:
            cmd.append("-F")  # fixed-string (literal) mode
        if include_glob:
            cmd.extend(["--glob", include_glob])

        rg_cwd: str | None = None
        if base_full.is_dir():
            cmd.extend(["--", pattern, "."])
            rg_cwd = str(base_full)
        else:
            cmd.extend(["--", pattern, str(base_full)])

        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=_GREP_TIMEOUT,
                check=False,
                cwd=rg_cwd,
            )
        except subprocess.TimeoutExpired:
            return None, False
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            # rg resolved at cache time but failed to exec — re-probe next call.
            _resolve_ripgrep_path.cache_clear()
            return None, False

        # 0 = match, 1 = no match (both fine); 2+ = hard error → fall back.
        if proc.returncode not in (0, 1):
            return None, False

        results: dict[str, list[tuple[int, str]]] = {}
        base_resolved = base_full.resolve()
        # On Windows, proc.stdout can be None even with a 0/1 returncode.
        # Treat that as "no matches" rather than crashing on splitlines().
        if not proc.stdout:
            return results, False
        for line in proc.stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") != "match":
                continue
            pdata = data.get("data", {})
            ftext = pdata.get("path", {}).get("text")
            if not ftext:
                continue
            raw = Path(ftext)
            p = raw if raw.is_absolute() else (base_full / raw)
            # Containment guard: drop any result that resolves outside the root.
            try:
                resolved = p.resolve()
                resolved.relative_to(base_resolved)
            except (ValueError, OSError):
                continue
            if self.virtual_mode:
                try:
                    # Already canonicalized for containment above. Resolving it
                    # again performs another Windows filesystem round trip per
                    # match; derive the identical virtual path from this result.
                    virt = "/" + resolved.relative_to(self.cwd).as_posix()
                except (ValueError, OSError, RuntimeError):
                    continue
            else:
                virt = str(p)
            ln = pdata.get("line_number")
            if ln is None:
                continue
            lt = pdata.get("lines", {}).get("text", "").rstrip("\n")
            results.setdefault(virt, []).append((int(ln), lt))

        # ponytail: cap applied after collection, not by killing rg mid-stream as
        # upstream does. Same visible contract (exactly max_count, flagged
        # truncated); only the peak-memory bound is looser. Stream it if a huge
        # match set ever actually hurts.
        return _cap_results(results, max_count)

    def _python_search(
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        *,
        max_count: int | None = None,
        timeout: int = _GREP_TIMEOUT,
    ):
        """Version-adaptive override the parent ``grep()`` calls.

        ``(results, truncated, error)`` on deepagents >= 0.7, ``(results, error)``
        on 0.6.x. Nova's own code should call :meth:`_py_impl`.
        """
        results, truncated, error = self._py_impl(
            pattern, base_full, include_glob, max_count=max_count, timeout=timeout
        )
        return (results, truncated, error) if _PARENT_PY_V2 else (results, error)

    def _py_impl(  # noqa: PLR0912
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        *,
        max_count: int | None = None,
        timeout: int = _GREP_TIMEOUT,
    ) -> tuple[dict[str, list[tuple[int, str]]], bool, str | None]:
        """Python fallback that prunes large directories to avoid hanging.

        Drop-in replacement for the upstream ``_python_search`` (same signature,
        so the parent ``grep()`` calls this automatically). The only behavioural
        difference is that subtrees in :data:`_SKIP_DIRS` are never descended
        into, which is what kept the upstream version from completing on repos
        with a vendored ``.venv`` / ``node_modules``.

        Args:
            pattern: Regex pattern. Callers pass ``re.escape(...)`` for a literal
                search (the parent does this) or a raw regex for regex mode.
            base_full: Resolved base path to search.
            include_glob: Optional glob to filter files.
            max_count: Optional cap on total matches collected.
            timeout: Wall-clock budget in seconds.

        Returns:
            ``(results, truncated, partial_error)`` — the upstream contract the
            parent ``grep()`` unpacks. ``truncated`` is True when ``max_count``
            dropped matches; ``partial_error`` is set when the walk was cut short
            by the timeout or aborted mid-iteration.
        """
        deadline = time.monotonic() + timeout
        # Smart-case to mirror the ripgrep path: case-insensitive unless the
        # pattern carries an upper-case letter. `pattern` is already re.escape'd
        # for literal searches, which never adds upper-case, so the check is safe.
        smart_flags = re.IGNORECASE if pattern == pattern.lower() else 0
        regex = re.compile(pattern, smart_flags)
        results: dict[str, list[tuple[int, str]]] = {}
        root = base_full if base_full.is_dir() else base_full.parent

        def _timed_out() -> str:
            return (
                f"Grep of '{base_full}' timed out after {timeout}s with "
                f"{len(results)} matching file(s); try a more specific pattern "
                f"or a narrower path."
            )

        # Collect one past the cap so truncation can be reported exactly (a run
        # that lands on max_count with nothing left is complete, not truncated).
        probe = None if max_count is None else max_count + 1
        matched = 0

        try:
            for dirpath, dirnames, filenames in os.walk(str(root)):
                if probe is not None and matched >= probe:
                    break
                if time.monotonic() > deadline:
                    capped, trunc = _cap_results(results, max_count)
                    return capped, trunc, _timed_out()

                # Prune in place so os.walk never descends into skipped subtrees.
                dirnames[:] = [
                    d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")
                ]

                for filename in filenames:
                    if probe is not None and matched >= probe:
                        break
                    if time.monotonic() > deadline:
                        capped, trunc = _cap_results(results, max_count)
                        return capped, trunc, _timed_out()

                    fp = Path(dirpath) / filename

                    if include_glob:
                        rel_path = str(fp.relative_to(root))
                        if not wcglob.globmatch(
                            rel_path, include_glob, flags=wcglob.BRACE | wcglob.GLOBSTAR
                        ):
                            continue

                    try:
                        if fp.stat().st_size > self.max_file_size_bytes:
                            continue
                    except (OSError, RuntimeError):
                        continue

                    try:
                        content = fp.read_text()
                    except (UnicodeDecodeError, PermissionError, OSError, RuntimeError):
                        continue

                    for line_num, line in enumerate(content.splitlines(), 1):
                        if probe is not None and matched >= probe:
                            break
                        if not regex.search(line):
                            continue
                        if self.virtual_mode:
                            try:
                                virt_path = self._to_virtual_path(fp)
                            except (ValueError, OSError, RuntimeError):
                                continue
                        else:
                            virt_path = str(fp)
                        results.setdefault(virt_path, []).append((line_num, line))
                        matched += 1

        except (OSError, RuntimeError) as e:
            msg = f"Grep of '{base_full}' aborted after {len(results)} matching file(s): {e}"
            capped, trunc = _cap_results(results, max_count)
            return capped, trunc, msg

        capped, trunc = _cap_results(results, max_count)
        return capped, trunc, None

    # -- read: recover non-utf-8 files ---------------------------------------

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read a file, recovering from a utf-8 decode failure.

        Upstream opens text files as utf-8 only, so a cp1252 / latin-1 / UTF-16
        file returns a decode error and reads *nothing*. On that error we re-read
        the bytes through a short encoding ladder (``latin-1`` always succeeds),
        returning the same windowed ``ReadResult`` the tool expects.
        """
        result = super().read(file_path, offset, limit)
        if result.error and "codec can't decode" in result.error:
            recovered = self._read_fallback_encoding(file_path, offset, limit)
            if recovered is not None:
                return recovered
        return result

    def _read_fallback_encoding(
        self, file_path: str, offset: int, limit: int
    ) -> ReadResult | None:
        """Re-read ``file_path`` trying non-utf-8 encodings; ``None`` if all fail."""
        try:
            raw = self._resolve_path(file_path).read_bytes()
        except (OSError, RuntimeError, ValueError):
            return None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                content = raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
            lines = content.splitlines(keepends=True)
            if lines and offset >= len(lines):
                return ReadResult(
                    error=f"Line offset {offset} exceeds file length ({len(lines)} lines)"
                )
            end = min(offset + limit, len(lines))
            windowed = "".join(lines[offset:end])
            return ReadResult(file_data={"content": windowed, "encoding": "utf-8"})
        return None

    # -- edit: actionable failure feedback -----------------------------------

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        """Edit a file, adding a recovery hint when the match fails.

        The upstream error ("String not found" / "appears N times") tells the
        model *that* it failed but not how to fix it. We append a hint — the
        closest line for a miss, or a replace_all/disambiguation tip for an
        ambiguous match — so the next attempt usually lands.
        """
        result = super().edit(file_path, old_string, new_string, replace_all)
        if not result.error:
            return result
        hint = self._edit_failure_hint(file_path, old_string, result.error)
        if hint:
            return EditResult(error=f"{result.error}\n\n{hint}")
        return result

    def _edit_failure_hint(self, file_path: str, old_string: str, error: str) -> str | None:
        """Build a one-line recovery hint for a failed edit (``None`` if none)."""
        low = error.lower()
        if "appears" in low and "times" in low:
            return (
                "Hint: pass replace_all=True to change every occurrence, or extend "
                "old_string with surrounding lines so it matches exactly one place."
            )
        if "not found" not in low:
            return None
        try:
            content = self._resolve_path(file_path).read_text(encoding="utf-8", errors="replace")
        except (OSError, RuntimeError, ValueError):
            return None
        first = next((ln for ln in old_string.splitlines() if ln.strip()), "")
        needle = re.sub(r"\s+", " ", first).strip()
        if not needle:
            return None
        # Whitespace-insensitive locate: indentation / internal-spacing drift is
        # the most common reason an otherwise-correct old_string doesn't match.
        for i, line in enumerate(content.splitlines(), 1):
            norm = re.sub(r"\s+", " ", line).strip()
            if norm and needle in norm:
                return (
                    f"Hint: no exact match, but line {i} is close:\n  {i}: {line}\n"
                    "old_string must match byte-for-byte (indentation, quotes, trailing "
                    f"spaces). Re-read around line {i} and copy the exact text."
                )
        return None

    # -- glob: prune noise during the walk + newest-first ---------------------

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Glob with vendored dirs pruned *during* the walk, newest-first.

        Upstream calls ``search_path.rglob(pattern)``, which descends into every
        subtree and ``stat()``s every hit before anything can be filtered. On a
        repo with a vendored ``.venv`` / ``node_modules`` that is tens of
        thousands of files (26k here vs 1.8k real ones), which blows the 20s
        ``GLOB_TIMEOUT`` in deepagents' glob tool.

        Filtering afterwards, as this method used to, still paid the full cost.
        We walk with ``os.walk`` and prune :data:`_SKIP_DIRS` from ``dirnames``
        in place, so those subtrees are never descended into — the same fix
        :meth:`_py_impl` already applies to grep. Results are ordered by mtime so
        the files the user is likely working on come first.
        """
        if self.virtual_mode and ".." in Path(pattern).parts:
            msg = "Path traversal not allowed in glob pattern"
            raise ValueError(msg)

        try:
            search_path = self.cwd if path in (None, "/") else self._resolve_path(path)
            if not search_path.exists() or not search_path.is_dir():
                return GlobResult(matches=[])
        except (OSError, RuntimeError, ValueError) as e:
            return GlobResult(error=f"Error globbing path '{path}': {e}", matches=[])

        # rglob(p) matches p at any depth; wcglob needs that spelled out.
        match_pat = pattern.lstrip("/")
        if not match_pat.startswith("**/"):
            match_pat = "**/" + match_pat
        flags = wcglob.BRACE | wcglob.GLOBSTAR

        matches: list[dict] = []
        try:
            for dirpath, dirnames, filenames in os.walk(str(search_path)):
                dirnames[:] = [
                    d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")
                ]
                for name in filenames:
                    full = Path(dirpath) / name
                    try:
                        rel = full.relative_to(search_path).as_posix()
                    except ValueError:
                        continue
                    if not wcglob.globmatch(rel, match_pat, flags=flags):
                        continue
                    if self.virtual_mode:
                        try:
                            out_path = self._to_virtual_path(full)
                        except (OSError, RuntimeError, ValueError):
                            continue
                    else:
                        out_path = str(full)
                    try:
                        st = full.stat()
                        matches.append(
                            {
                                "path": out_path,
                                "is_dir": False,
                                "size": int(st.st_size),
                                "_mtime": st.st_mtime,
                            }
                        )
                    except OSError:
                        matches.append({"path": out_path, "is_dir": False, "_mtime": 0.0})
        except (OSError, RuntimeError, ValueError) as e:
            return GlobResult(
                error=f"Glob of '{path}' aborted partway: {e}",
                matches=[{k: v for k, v in m.items() if k != "_mtime"} for m in matches],
            )

        matches.sort(key=lambda m: m["_mtime"], reverse=True)
        for m in matches:
            del m["_mtime"]
        return GlobResult(matches=matches)


class ConversationHistoryBackend(FilesystemBackend):
    """Write-only store for conversation history evicted by summarization.

    The summarization middleware offloads replaced history here and tells the
    model it can "recover the full text by reading the offloaded file". Acting on
    that advice re-inhales the exact context eviction just cleared, which pushes
    usage back over the summarization threshold and summarizes again — the
    repeating "SESSION INTENT / task not recoverable" loop.

    Writes still land on disk, so the transcript stays available to the user and
    to `/resume`; only the agent-facing *read* is refused, which is what breaks
    the cycle. Deterministic on purpose: a prompt-level "don't re-read this" can
    be ignored by the model, a refused read cannot.
    """

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Refuse the read with a short instruction instead of the transcript."""
        return ReadResult(
            error=(
                "This file holds earlier conversation history that was already "
                "summarized out of the active context. Re-reading it would undo "
                "that compaction and re-trigger summarization, so it is not "
                "readable. Continue from the summary you have; if the goal is "
                "unclear, ask the user rather than trying to reconstruct it."
            )
        )


class OptimizedLocalShellBackend(LocalShellBackend, OptimizedFilesystemBackend):
    """A ``LocalShellBackend`` that uses Nova's guarded, non-hanging grep.

    Nova's local-mode composite default backend must support the ``execute``
    tool (so subagents can run commands), which is why deepagents'
    :class:`LocalShellBackend` is used. But ``LocalShellBackend`` subclasses the
    *base* deepagents ``FilesystemBackend``, whose ``_ripgrep_search`` lacks the
    Windows None-stdout guard and whose Python fallback walks ``.venv`` /
    ``node_modules`` (the hang this package fixes). The MRO here —
    ``LocalShellBackend`` → :class:`OptimizedFilesystemBackend` → ``FilesystemBackend``
    — keeps ``execute`` from the former while resolving ``grep`` /
    ``_ripgrep_search`` to the optimized, guarded versions. No new behaviour of
    its own.
    """
