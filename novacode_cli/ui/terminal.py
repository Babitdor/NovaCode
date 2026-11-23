"""A shell command in a real TTY, streamed to a client as plain lines.

The terminal UI runs `!!cmd` in its own screen and gives the child a real
terminal, so prompts, colours and progress bars behave. A socket client cannot
share a file descriptor with the server, so this module owns the loop instead:
it spawns the same [`PtyShell`][novacode_cli.ui.pty_shell.PtyShell], drains the
output, and hands the client plain text made of whole lines.

Text, not cells, is the deliberate limit: a cell grid needs a terminal emulator
(cursor addressing, scroll regions, alternate screens) and that is a separate
row in `rust/Plan.md`. Until then `vim` and `less` will not look right, while
`pwd`, `ls`, `git status`, test runs and build logs read correctly.

Escape sequences are removed server-side with [`strip_ansi`][novacode_cli.shell.output.strip_ansi]
so one implementation decides what "plain" means for both frontends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.shell.output import strip_ansi
from novacode_cli.ui.pty_shell import PtyShell

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["Terminal", "TerminalUnavailableError"]

#: Output kept for a client that joins late or scrolls back.
MAX_LINES = 4000
#: How long a read waits before the pump sleeps, in seconds.
POLL_SECONDS = 0.05


class TerminalUnavailableError(RuntimeError):
    """The platform's pseudo-terminal backend is missing or refused the command."""


class Terminal:
    """One shell command in a TTY, with its output as plain lines.

    Args:
        shell: The spawned [`PtyShell`][novacode_cli.ui.pty_shell.PtyShell].
        command: The command line, shown in the client's header.
    """

    def __init__(self, shell: PtyShell, command: str) -> None:
        """Wrap an already-spawned shell.

        Args:
            shell: The spawned pseudo-terminal.
            command: The command line, kept for the client's header.
        """
        self._shell = shell
        self._command = command
        self._lines: list[str] = []
        self._pending: list[str] = []
        self._partial = ""
        self._pending_cr = False
        self._code: int | None = None
        self._closed = False

    @classmethod
    async def spawn(
        cls,
        command: str,
        cwd: Path,
        *,
        rows: int = 24,
        columns: int = 100,
    ) -> Terminal:
        """Start *command* in a pseudo-terminal.

        Args:
            command: Shell command line.
            cwd: Working directory for the child.
            rows: Terminal rows.
            columns: Terminal columns.

        Returns:
            A terminal whose output still has to be drained.

        Raises:
            TerminalUnavailable: When the platform backend is missing.
        """
        try:
            shell = await PtyShell.spawn(command, cwd, rows=rows, columns=columns)
        except Exception as exc:
            raise TerminalUnavailableError(str(exc)) from exc
        return cls(shell, command)

    @property
    def command(self) -> str:
        """The command line, for the client's header."""
        return self._command

    @property
    def finished(self) -> bool:
        """Whether the child has exited and every line has been taken."""
        return self._closed and self._code is not None

    @property
    def exit_code(self) -> int | None:
        """The child's status once it has exited."""
        return self._code

    @property
    def output(self) -> str:
        """Everything read so far, as plain text."""
        return "\n".join(self._lines)

    async def drain(self) -> str:
        """Read what arrived and return the complete lines, joined by newlines.

        A trailing partial line is held back until its newline arrives, so the
        client never has to splice half a line. A carriage return rewrites the
        line, which is what makes a progress bar end up as its final state.

        Returns:
            The new lines (empty when nothing arrived).
        """
        if self._closed:
            return ""
        chunk = await self._shell.read()
        if chunk is None:
            return ""
        if chunk == "":
            # EOF: flush the partial line and remember how the child exited.
            self._code = await self._shell.wait()
            self._closed = True
            tail = self._partial
            self._partial = ""
            if tail:
                self._lines.append(tail)
                self._pending.append(tail)
            return self._flush()
        self._absorb(strip_ansi(chunk))
        return self._flush()

    def _absorb(self, text: str) -> None:
        """Fold *text* into whole lines, honouring carriage returns and backspace.

        A carriage return is held until the next character arrives: followed by
        a newline it is an ordinary line break (CRLF, which is all Windows
        produces), and followed by anything else it rewrites the line in place,
        which is how a progress bar reports itself.
        """
        for glyph in text:
            if self._pending_cr:
                self._pending_cr = False
                if glyph == "\n":
                    self._newline()
                    continue
                self._partial = ""
            if glyph == "\r":
                self._pending_cr = True
            elif glyph == "\n":
                self._newline()
            elif glyph == "\b":
                self._partial = self._partial[:-1]
            elif glyph == "\x00":
                continue
            else:
                self._partial += glyph
        self._trim()

    def _newline(self) -> None:
        """Close the current line."""
        self._lines.append(self._partial)
        self._pending.append(self._partial)
        self._partial = ""

    def _trim(self) -> None:
        """Keep the output history bounded."""
        if len(self._lines) > MAX_LINES:
            del self._lines[:-MAX_LINES]

    def _flush(self) -> str:
        """Return the lines added since the last call, and forget them."""
        if not self._pending:
            return ""
        text = "\n".join(self._pending)
        self._pending.clear()
        return text

    def write(self, keys: str) -> None:
        r"""Send raw keys to the child.

        Args:
            keys: Bytes as text; the client sends `\r` for Enter.
        """
        if self._closed:
            return
        self._shell.write(keys)

    def resize(self, rows: int, columns: int) -> None:
        """Resize the child's terminal, when the backend supports it."""
        resize = getattr(self._shell, "resize", None)
        if callable(resize):
            resize(rows, columns)

    async def stop(self) -> None:
        """Stop the child and release its handle."""
        if self._closed:
            return
        self._closed = True
        self._code = 0 if self._code is None else self._code
        try:
            self._shell.terminate()
        finally:
            self._shell.close()
