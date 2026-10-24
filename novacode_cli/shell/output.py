"""What a shell command's result tells the model: output window and exit status.

* **Middle-out window.** A long build or test run keeps its first and last
  halves; the middle is dropped and counted. Cutting the end (the old
  behaviour) lost the final error and the test summary, which is the part the
  model needs. The full output is saved to a log file named in the result.
* **Honest exit codes.** A signal death is reported as ``128+N`` with the
  signal's name, instead of a bare negative number. ``grep``/``rg`` exiting 1
  (no matches) and ``diff``/``cmp`` exiting 1 (files differ) are answers, not
  failures, so they are annotated and not marked as errors.
"""

from __future__ import annotations

import contextlib
import re
import signal
from typing import TYPE_CHECKING, TextIO
from uuid import uuid4

from novacode_cli.config import config as _config

if TYPE_CHECKING:
    from pathlib import Path

# ponytail: keep-newest pruning on each new log; switch to age-based cleanup if
# many concurrent sessions need longer history.
MAX_SHELL_LOGS = 50

_NO_MATCH_TOOLS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "findstr", "select-string"})
_DIFF_TOOLS = frozenset({"diff", "cmp", "fc"})
_COMPOUND = re.compile(r"&&|\|\||;|\n")


def shell_log_dir() -> Path:
    """Where full outputs of truncated commands are kept."""
    return _config.HOME_DIR / "logs" / "shell"


def open_shell_log() -> tuple[TextIO, Path] | None:
    """A new log file for one command's full output, or None if it can't be created."""
    directory = shell_log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        old = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime)
        for stale in old[: max(0, len(old) - MAX_SHELL_LOGS + 1)]:
            with contextlib.suppress(OSError):
                stale.unlink()
        path = directory / f"{uuid4().hex[:12]}.log"
        return path.open("w", encoding="utf-8", errors="replace"), path
    except OSError:
        return None


class OutputWindow:
    """Bounded capture keeping the head and a rolling tail of a command's output."""

    def __init__(self, max_chars: int) -> None:
        """Keep at most ``max_chars`` in memory, split between head and tail."""
        self._half = max(1, max_chars // 2)
        self._head: list[str] = []
        self._head_chars = 0
        self._tail = ""
        self.omitted = 0
        self._log: TextIO | None = None
        self.log_path: Path | None = None

    @property
    def spilled(self) -> bool:
        """Whether output went past the head (the tail is in use)."""
        return bool(self._tail) or self.omitted > 0

    def add(self, text: str) -> None:
        """Append a chunk of output."""
        if self._head_chars < self._half:
            part = text[: self._half - self._head_chars]
            self._head.append(part)
            self._head_chars += len(part)
            text = text[len(part) :]
            if not text:
                return
        if self._log is not None:
            try:
                self._log.write(text)
            except OSError:
                self._log = None
        self._tail += text
        if len(self._tail) > self._half:
            if self._log is None and self.log_path is None:
                # First cut: nothing is lost yet, so the log starts complete.
                opened = open_shell_log()
                if opened is not None:
                    self._log, self.log_path = opened
                    with contextlib.suppress(OSError):
                        self._log.write("".join(self._head) + self._tail)
            self.omitted += len(self._tail) - self._half
            self._tail = self._tail[-self._half :]

    def close(self) -> None:
        """Flush the full-output log."""
        if self._log is not None:
            with contextlib.suppress(OSError):
                self._log.close()
            self._log = None

    def text(self) -> str:
        """The head, an omission note if the middle was dropped, and the tail."""
        self.close()
        head = "".join(self._head)
        if not self.omitted:
            return head + self._tail
        where = f" Full output: {self.log_path}" if self.log_path else ""
        return (
            f"{head}\n\n[... {self.omitted} characters omitted from the middle.{where} ...]\n\n"
            f"{self._tail}"
        )


def _last_program(command: str) -> str | None:
    """The program whose status a simple command or pipeline reports, else None."""
    if _COMPOUND.search(command):
        return None
    words = [w for w in command.strip().split("|")[-1].split() if not re.match(r"\w+=", w)]
    if not words:
        return None
    name = re.split(r"[\\/]", words[0].strip("\"'"))[-1].lower()
    return name.removesuffix(".exe")


def exit_status(command: str, code: int | None) -> tuple[str, bool]:
    """``(note, failed)`` for a finished command; the note is empty for success."""
    if code in (0, None):
        return "", False
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"Exit code: {128 - code} (killed by {name})", True
    program = _last_program(command)
    if code == 1 and program in _NO_MATCH_TOOLS:
        return f"No matches found ({program} exit code 1).", False
    if code == 1 and program in _DIFF_TOOLS:
        return f"Files differ ({program} exit code 1).", False
    return f"Exit code: {code}", True
