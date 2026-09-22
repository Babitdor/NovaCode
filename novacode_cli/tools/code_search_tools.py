"""Semble-powered semantic code search tools for the Nova-Code agent.

Provides two LangChain tools backed by the `semble` library:

- ``code_search``: Natural-language or symbol-based code search that returns
  only the relevant code chunks (file path, line range, content) instead of
  entire files.  Uses ~98 % fewer tokens than grep+read.
- ``find_related_code``: Given a file path and line number from a prior
  ``code_search`` result, return semantically similar code chunks.

The Semble index is created lazily on first use, cached for the session
lifetime, and automatically re-indexed when files change.

If the optional ``semble`` package is not installed the tools degrade
gracefully, returning a message that the feature is unavailable.

Technical details
-----------------
Semble uses two complementary retrievers fused with Reciprocal Rank Fusion:
  - **Semantic**: static Model2Vec embeddings (potion-code-16M, 16M params)
  - **Lexical**: BM25 on identifiers and API names

After fusion, code-aware reranking signals are applied:
  - Adaptive weighting (symbol queries → more BM25 weight)
  - Definition boosts (class/def/func definitions rank above references)
  - Identifier stems (parseConfig matches "parse config")
  - File coherence (multi-match files boosted)
  - Noise penalties (tests, compat/ shims down-ranked)

Everything runs on CPU with **no API keys, GPU, or external services**.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from langchain.tools import tool

# Silence the HuggingFace Hub symlink warning on Windows.
# On Windows, the HF cache can't use symlinks unless Developer Mode is on.
# The warning is verbose and irrelevant for Semble's static model download.
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal state — single-session Semble index
# ---------------------------------------------------------------------------

_index: Any = None  # semble.SembleIndex | None
_index_root: Path | None = None
_index_mtime: float = 0.0  # last modification time of the index snapshot
_index_created_at: float = 0.0  # wall-clock time when the index was built
_REINDEX_TTL = 300.0  # seconds; re-index if older than this and files changed

# Flag to suppress repeated "semble not installed" warnings.
_semble_unavailable_warned = False


def _is_semble_available() -> bool:
    """Return True if the ``semble`` package can be imported."""
    try:
        import semble  # noqa: F401

        return True
    except ImportError:
        return False


def _reset_index() -> None:
    """Force a full re-index on the next ``code_search`` / ``find_related_code`` call.

    Called by the ``/reindex`` command and after significant file changes.
    """
    global _index, _index_root, _index_mtime, _index_created_at
    _index = None
    _index_root = None
    _index_mtime = 0.0
    _index_created_at = 0.0


def _get_index(workspace_root: Path) -> Any:
    """Get or create the Semble index for the project.

    The index is built lazily on first call and cached for the session.
    If more than ``_REINDEX_TTL`` seconds have passed and the project's
    file modification time has changed, the index is rebuilt automatically.

    Args:
        workspace_root: Path to the project root directory.

    Returns:
        A ``semble.SembleIndex`` instance, or None if semble is unavailable.
    """
    global _index, _index_root, _index_mtime, _index_created_at, _semble_unavailable_warned

    if not _is_semble_available():
        if not _semble_unavailable_warned:
            logger.debug(
                "semble package not installed — code_search tools unavailable. "
                "Install with: pip install semble"
            )
            _semble_unavailable_warned = True
        return None

    from semble import SembleIndex

    now = time.time()

    # Return cached index if root matches and not stale
    if _index is not None and _index_root == workspace_root:
        cache_age = now - _index_created_at
        if cache_age < _REINDEX_TTL:
            return _index
        # TTL expired — check if any source files actually changed
        # (cheap heuristic: check if the most-recently-modified .py/.ts/.js
        #  file under the workspace is newer than our index snapshot)
        try:
            _marker = _newest_source_mtime(workspace_root)
            if _marker <= _index_mtime:
                # No files changed since last index — extend TTL
                _index_created_at = now
                return _index
        except Exception:
            pass  # fallback: re-index

    # Build a fresh index
    try:
        t0 = now
        _first_time = _index is None
        if _first_time:
            logger.info("Building code search index for %s (first time)...", workspace_root)
        _index = SembleIndex.from_path(str(workspace_root))
        _index_root = workspace_root
        _index_created_at = time.time()
        _index_mtime = _newest_source_mtime(workspace_root)
        elapsed_ms = (_index_created_at - t0) * 1000
        if _first_time or elapsed_ms > 1000:
            logger.info(
                "Semble index built for %s in %.0f ms",
                workspace_root,
                elapsed_ms,
            )
    except Exception as exc:
        logger.warning("Failed to build Semble index: %s", exc)
        _index = None
        return None

    return _index


def _newest_source_mtime(root: Path) -> float:
    """Return the modification time of the most recently changed source file.

    Walks the top two directory levels (fast heuristic) and checks common
    source extensions.  Returns 0.0 if nothing is found.

    Args:
        root: Project root directory.

    Returns:
        ``st_mtime`` of the newest source file, or 0.0.
    """
    _SOURCE_EXTS = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
        ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift",
        ".kt", ".scala", ".sh", ".bash", ".lua", ".r", ".m", ".sql",
    }
    newest = 0.0
    try:
        for dirpath, _dirnames, filenames in root.walk():
            # Limit depth for speed (3 levels is enough for most projects)
            depth = len(dirpath.relative_to(root).parts)
            if depth > 3:
                continue
            # Skip hidden, .git, node_modules, __pycache__, .venv
            _dirnames[:] = [
                d for d in _dirnames
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", ".venv", "venv")
            ]
            for fname in filenames:
                if Path(fname).suffix.lower() in _SOURCE_EXTS:
                    try:
                        mtime = (dirpath / fname).stat().st_mtime
                        if mtime > newest:
                            newest = mtime
                    except OSError:
                        continue
    except Exception:
        pass
    return newest


# ---------------------------------------------------------------------------
# LangChain Tools
# ---------------------------------------------------------------------------


@tool
def code_search(query: str, top_k: int = 5) -> str:
    """Search the codebase for code matching a natural-language or symbol query.

    Returns relevant code snippets (file path, line numbers, content) without
    reading entire files.  Uses semantic search + lexical matching, so it
    finds code even when the query phrasing differs from the code's naming.

    Use this tool when:
    - You need to find WHERE a feature is implemented ("how is auth handled?")
    - You want to find code by description, not just by exact name
    - You want to find a specific class/function but only know part of its name
    - You want to discover similar implementations across the codebase

    Do NOT use this tool when:
    - You need the full file for extensive edits (use read_file instead)
    - You need exhaustive/exact string matches (use grep instead)
    - You need a filename match rather than a code match (use glob instead)

    Args:
        query: Natural-language description or symbol name to search for.
               Examples: "authentication flow", "save_pretrained",
               "where are LLM calls made", "rate limiting middleware".
        top_k: Maximum number of results to return (default 5, max 20).

    Returns:
        Matching code snippets with file paths and line numbers, or an
        error/unavailable message.
    """
    from novacode_cli.config.config import settings

    workspace_root = settings.project_root or Path.cwd()
    top_k = max(1, min(top_k, 20))

    index = _get_index(workspace_root)
    if index is None:
        return (
            "Code search unavailable — the 'semble' package is not installed. "
            "Install it with: pip install semble"
        )

    try:
        results = index.search(query, top_k=top_k)
    except Exception as exc:
        logger.debug("Semble search failed: %s", exc)
        return f"Code search error: {exc}"

    if not results:
        return f"No code found matching '{query}'."

    lines: list[str] = [f"Code search: '{query}' ({len(results)} result(s))\n"]
    for i, result in enumerate(results, 1):
        chunk = result.chunk
        # Normalize path: backslashes → forward slashes for virtual-path compat
        rel_path = chunk.file_path.replace("\\", "/")
        ext = Path(rel_path).suffix.lstrip(".") or "text"
        lines.append(f"### {i}. `/{rel_path}:{chunk.start_line}-{chunk.end_line}`")
        lines.append(f"```{ext}")
        lines.append(chunk.content)
        lines.append("```\n")
    return "\n".join(lines)


@tool
def find_related_code(file_path: str, line: int, top_k: int = 3) -> str:
    """Find code semantically similar to a known location in the codebase.

    Given a file path and line number (from a prior code_search result or
    any known location), return chunks with similar logic, patterns, or
    implementation approaches.

    Use this tool when:
    - You found a relevant result via code_search and want to see similar code
    - You're looking for alternative implementations of the same pattern
    - You want to discover related code that might need the same change

    Args:
        file_path: Path to the reference file (relative to project root,
                   or virtual path like /src/main.py).
        line: Line number in the reference file.
        top_k: Maximum number of related chunks to return (default 3, max 10).

    Returns:
        Code chunks semantically similar to the reference location, or an
        error/unavailable message.
    """
    from novacode_cli.config.config import settings

    workspace_root = settings.project_root or Path.cwd()
    top_k = max(1, min(top_k, 10))

    # Normalize virtual paths (e.g. /src/main.py → src/main.py)
    clean_path = file_path.lstrip("/")

    index = _get_index(workspace_root)
    if index is None:
        return (
            "Code search unavailable — the 'semble' package is not installed. "
            "Install it with: pip install semble"
        )

    try:
        # Semble's find_related takes a SearchResult object, not (path, line).
        # We perform a targeted search first to get a SearchResult at the
        # given location, then call find_related on it.
        search_results = index.search(f"file:{clean_path}", top_k=5)
        anchor = None
        # Compare paths in a platform-agnostic way (normalize separators)
        clean_norm = clean_path.replace("\\", "/")
        for sr in search_results:
            sr_path_norm = sr.chunk.file_path.replace("\\", "/")
            if sr_path_norm == clean_norm and sr.chunk.start_line <= line <= sr.chunk.end_line:
                anchor = sr
                break
            # Also match if the file matches and we're close
            if sr_path_norm == clean_norm and abs(sr.chunk.start_line - line) <= 30:
                anchor = sr
                break

        # Fallback: if the file exists in results at all, use the first match
        if anchor is None:
            for sr in search_results:
                sr_path_norm = sr.chunk.file_path.replace("\\", "/")
                if sr_path_norm == clean_norm:
                    anchor = sr
                    break

        # Last resort: search by the file path as the query itself
        if anchor is None:
            fallback_results = index.search(clean_path, top_k=1)
            if fallback_results:
                anchor = fallback_results[0]

        if anchor is None:
            return f"Could not locate '{clean_path}:{line}' in the index. Try code_search instead."

        related = index.find_related(anchor, top_k=top_k)
    except Exception as exc:
        logger.debug("Semble find_related failed: %s", exc)
        return f"Find related error: {exc}"

    if not related:
        return f"No related code found for {file_path}:{line}."

    lines: list[str] = [f"Related to `{file_path}:{line}` ({len(related)} result(s))\n"]
    for i, result in enumerate(related, 1):
        chunk = result.chunk
        # Normalize path: backslashes → forward slashes for virtual-path compat
        rel_path = chunk.file_path.replace("\\", "/")
        ext = Path(rel_path).suffix.lstrip(".") or "text"
        lines.append(f"### {i}. `/{rel_path}:{chunk.start_line}-{chunk.end_line}`")
        lines.append(f"```{ext}")
        lines.append(chunk.content)
        lines.append("```\n")
    return "\n".join(lines)