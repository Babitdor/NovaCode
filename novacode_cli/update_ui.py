"""``nova update``'s terminal UI: stage spinners, a live output line, the finish screen.

Standard library only, so the update path imports nothing heavy. Off a
terminal (piped, CI) it prints plain lines with no escape codes.
"""

# Explicit terminal output and subprocess argument lists.
# ruff: noqa: T201, S603

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FROM, _TO = (122, 162, 247), (187, 154, 247)  # Nova blue -> violet
_GREEN, _RED, _DIM, _BOLD, _RESET = "\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[1m", "\x1b[0m"


def enable_ansi(stream: TextIO) -> bool:
    """True when ``stream`` is a terminal that renders escape codes."""
    if not stream.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(-12 if stream is sys.stderr else -11)
        mode = wintypes.DWORD()
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 0x0004)  # virtual terminal
        )
    except (OSError, AttributeError):
        return False


def _rgb(fraction: float) -> str:
    r, g, b = (round(a + (b - a) * fraction) for a, b in zip(_FROM, _TO, strict=True))
    return f"\x1b[38;2;{r};{g};{b}m"


def _gradient(text: str) -> str:
    width = max(len(text) - 1, 1)
    return "".join(_rgb(i / width) + ch for i, ch in enumerate(text)) + _RESET


class CommandFailed(subprocess.CalledProcessError):
    """A step's command failed; ``output`` holds everything it printed."""


class Progress:
    """One line per stage: a spinner and the command's latest output while it
    runs, then ``✓ stage  note  2.1s``. Use as a context manager."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.out = stream or sys.stdout
        self.tty = enable_ansi(self.out)
        self._title: str | None = None
        self._tail = ""
        self._started = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Progress:
        if self.tty:
            self.out.write("\x1b[?25l")  # hide the cursor while spinning
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, kind: object, error: object, _tb: object) -> None:
        if self._title is not None:
            self.done() if kind is None else self.fail()
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self.tty:
            self.out.write("\x1b[?25h")
            self.out.flush()

    def stage(self, title: str) -> None:
        if self._title is not None:
            self.done()
        with self._lock:
            self._title, self._tail, self._started = title, "", time.monotonic()
        if not self.tty:
            print(f"- {title}...", file=self.out, flush=True)

    def line(self, text: str) -> None:
        """The running command printed ``text``: show it dimmed after the spinner."""
        self._tail = " ".join(text.split())

    def done(self, note: str = "") -> None:
        self._finish(f"{_GREEN}✓{_RESET}", "done", note)

    def fail(self, note: str = "") -> None:
        self._finish(f"{_RED}✗{_RESET}", "failed", note)

    def _finish(self, mark: str, word: str, note: str) -> None:
        with self._lock:
            title, self._title = self._title, None
            if title is None:
                return
            if not self.tty:
                print(f"  {word}: {title}" + (f" ({note})" if note else ""), file=self.out, flush=True)
                return
            took = f"{time.monotonic() - self._started:.1f}s"
            note = f"  {note}" if note else ""
            self.out.write(f"\r\x1b[2K  {mark} {title}{_DIM}{note}  {took}{_RESET}\n")
            self.out.flush()

    def _spin(self) -> None:
        frame = 0
        while not self._stop.wait(0.08):
            with self._lock:
                if self._title is None:
                    continue
                width = shutil.get_terminal_size((80, 24)).columns
                head = f"  {_FRAMES[frame % len(_FRAMES)]} {self._title}"
                room = width - len(head) - 4
                tail = self._tail[: max(room, 0)]
                spinner = _rgb((frame % 20) / 19 if frame % 40 < 20 else 1 - (frame % 20) / 19)
                self.out.write(
                    f"\r\x1b[2K  {spinner}{_FRAMES[frame % len(_FRAMES)]}{_RESET} "
                    f"{_BOLD}{self._title}{_RESET}  {_DIM}{tail}{_RESET}"
                )
                self.out.flush()
            frame += 1


def run_command(arguments: list[str], progress: Progress, *, cwd: Path | None = None) -> str:
    """Run a step's command, its output feeding the spinner line; return the output."""
    lines: list[str] = []
    with subprocess.Popen(
        arguments,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            if line.strip():
                progress.line(line)
        code = process.wait()
    output = "".join(lines)
    if code:
        raise CommandFailed(code, arguments, output=output)
    return output


def installed_version(python: str | None = None) -> str:
    """Read the updated environment, rather than an already-imported version."""
    if python:
        try:
            return subprocess.check_output(
                [python, "-I", "-c",
                 "from importlib.metadata import version; print(version('novacode-cli'))"],
                text=True, encoding="utf-8", stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        tree = ast.parse(Path(__file__).with_name("_version.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            ):
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    return value
    except (OSError, ValueError, SyntaxError):
        pass
    return "unavailable"


def show_success(
    old: str,
    new: str,
    logo: str = "NOVA",
    *,
    note: str = "",
    hint: str = "Run nova to start the new version.",
    stream: TextIO | None = None,
) -> None:
    """The finish screen: the logo sweeps in, then what changed and what to do next.

    It prints below the progress (the terminal's history stays), and returns
    straight to the shell prompt.
    """
    out = stream or sys.stdout
    tty = enable_ansi(out)
    rows = logo.splitlines()
    if any(len(row) > shutil.get_terminal_size((80, 24)).columns - 4 for row in rows):
        rows = ["N O V A"]
    change = f"v{old} → v{new}" if old and old != new else f"v{new}"
    lines = [
        ("check", f"NovaCode updated   {change}"),
        ("note", note),
        ("hint", hint),
    ]
    print(file=out)
    for row in rows:
        print("   " + (_gradient(row) if tty else row.rstrip()), file=out, flush=True)
        if tty:
            time.sleep(0.04)
    print(file=out)
    for kind, text in lines:
        if not text:
            continue
        if not tty:
            print(f"   {'OK ' if kind == 'check' else '   '}{text}", file=out)
            continue
        if kind == "check":
            out.write(f"   {_GREEN}✓{_RESET} ")
            for ch in text:  # typed out
                out.write(f"{_BOLD}{ch}{_RESET}")
                out.flush()
                time.sleep(0.012)
            out.write("\n")
        else:
            out.write(f"     {_DIM}{text}{_RESET}\n")
        out.flush()
    print(file=out, flush=True)
