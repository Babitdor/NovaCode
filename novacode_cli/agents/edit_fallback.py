"""Recover an ``edit_file`` whose ``old_string`` differs only in indentation.

deepagents 0.7 prints ``read_file`` lines as ``NN  code`` (two spaces), not
``NN<tab>code``. Models then fold the gutter's spaces into the code: every
``old_string`` line carries two extra spaces, the exact match fails, and the
model retries the same wrong string until a loop guard stops it (seen on the
fix-data-processing-bug eval). Codex's ``apply_patch`` handles this class of
error the same way: when the exact match fails, match lines by their
whitespace-trimmed text, and accept only a single, unambiguous hit.

The replacement then gets the same indentation shift the model got wrong, so
the edit lands at the file's real indentation. Anything unsure (several
matches, lines that disagree on the shift, tabs) returns ``None`` and the
original "String not found" error stands.
"""

from __future__ import annotations


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _shift(line: str, by: int) -> str | None:
    if not line.strip() or by == 0:
        return line
    if by > 0:
        return " " * by + line
    return line[-by:] if line[:-by].strip(" ") == "" else None


def indent_tolerant_replace(content: str, old_string: str, new_string: str) -> str | None:
    """``content`` with the single indentation-insensitive match replaced, else None."""
    old, new = old_string.removesuffix("\n"), new_string.removesuffix("\n")
    old_lines = old.split("\n")
    key = [line.strip() for line in old_lines]
    if not any(key):
        return None
    lines = content.split("\n")
    n = len(old_lines)
    hits = [i for i in range(len(lines) - n + 1) if [x.strip() for x in lines[i : i + n]] == key]
    if len(hits) != 1:
        return None
    start = hits[0]
    window = lines[start : start + n]
    shifts = {
        _indent(f) - _indent(o) for f, o in zip(window[1:], old_lines[1:], strict=True) if o.strip()
    }
    if len(shifts) > 1:
        return None
    rest = shifts.pop() if shifts else 0
    first = _indent(window[0]) - _indent(old_lines[0])
    if (rest or first) and any("\t" in w[: _indent(w)] for w in window):
        return None  # tab-indented file: a space shift would corrupt it
    eol = "\r" if window[0].endswith("\r") else ""
    out = []
    for j, line in enumerate(new.split("\n")):
        shifted = _shift(line.rstrip("\r"), first if j == 0 else rest)
        if shifted is None:
            return None
        out.append(shifted + eol)
    return "\n".join(lines[:start] + out + lines[start + n :])
