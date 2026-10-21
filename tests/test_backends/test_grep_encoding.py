"""Grep must survive files that are not decodable with the platform locale codec.

Regression: ripgrep's UTF-8 output was decoded with the locale codec (cp1252 on
Windows). A single non-ASCII character in a matched line (a curly quote is
``E2 80 9D``) raised ``UnicodeDecodeError: 'charmap' codec can't decode byte
0x9d`` and killed the whole search — which is what stopped the background dream
when it grepped the memory files.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import novacode_cli.backends.filesystem as fs_mod
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

CURLY_QUOTED = "the \u201dquoted\u201d line has a needle too"


def _write_utf8_note(tmp_path: Path) -> Path:
    note = tmp_path / "notes.md"
    note.write_text(f"alpha needle omega\n{CURLY_QUOTED}\n", encoding="utf-8")
    return note


# -- ripgrep path ------------------------------------------------------------


def test_grep_finds_matches_in_utf8_file_with_non_ascii(tmp_path: Path) -> None:
    """The ripgrep path must decode its output as UTF-8, not as cp1252."""
    _write_utf8_note(tmp_path)
    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)

    result = backend.grep("needle", path=".")

    assert result.error is None
    assert result.matches, "grep found no matches in a UTF-8 file"
    lines = [m["text"] for m in result.matches]
    assert any(CURLY_QUOTED in line for line in lines), lines


def test_rg_output_is_decoded_as_utf8(tmp_path: Path) -> None:
    """Pin the decoding of ripgrep's stdout to UTF-8."""
    _write_utf8_note(tmp_path)
    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)

    with mock.patch.object(fs_mod.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=1, stdout="")
        backend.grep("needle", path=".")

    assert run.called
    kwargs = run.call_args.kwargs
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"


# -- python fallback ---------------------------------------------------------


def test_python_fallback_finds_matches_in_utf8_file(tmp_path: Path) -> None:
    """Without ripgrep, the Python fallback must decode files as UTF-8 too."""
    _write_utf8_note(tmp_path)
    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)

    with mock.patch.object(fs_mod, "_resolve_ripgrep_path", return_value=None):
        result = backend.grep("needle", path=".")

    assert result.error is None
    assert result.matches, "the Python fallback found no matches in a UTF-8 file"
    lines = [m["text"] for m in result.matches]
    assert any(CURLY_QUOTED in line for line in lines), lines


def test_python_fallback_reads_cp1252_file(tmp_path: Path) -> None:
    """A cp1252 file must still be searched (decoded, not skipped)."""
    (tmp_path / "latin.txt").write_bytes("café needle\n".encode("cp1252"))
    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)

    with mock.patch.object(fs_mod, "_resolve_ripgrep_path", return_value=None):
        result = backend.grep("needle", path=".")

    assert result.error is None
    assert result.matches, "the Python fallback skipped a cp1252 file"
