"""The dream agent's memory backend must not decode with the locale codec.

Regression: the dream agent served ``/memories/`` with upstream deepagents'
``FilesystemBackend``, whose ripgrep path decodes search output with the
platform locale codec (cp1252 on Windows). Grepping a memory file that contains
a non-ASCII character (a curly quote is ``E2 80 9D``) raised
``UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d`` and stopped the
background dream.
"""

from __future__ import annotations

from pathlib import Path

from novacode_cli.agents.dream_agent import build_dream_backend
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend


def test_memory_route_uses_the_optimized_backend(tmp_path: Path) -> None:
    backend = build_dream_backend(tmp_path)
    route = backend.routes["/memories/"]
    assert isinstance(route, OptimizedFilesystemBackend)


def test_grep_over_memory_with_non_ascii_does_not_raise(tmp_path: Path) -> None:
    """Grepping a memory file with a curly quote must return matches, not raise."""
    (tmp_path / "notes.md").write_text(
        "alpha needle omega\nthe \u201dquoted\u201d line has a needle too\n",
        encoding="utf-8",
    )
    backend = build_dream_backend(tmp_path)

    result = backend.grep("needle", path="/memories/")

    assert result.error is None
    assert result.matches, "grep over the memory files found nothing"
    lines = [m["text"] for m in result.matches]
    assert any("\u201dquoted\u201d" in line for line in lines), lines
