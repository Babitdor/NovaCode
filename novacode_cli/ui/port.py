"""The narrow UI seam between server-side command handlers and a frontend.

Today every slash command prints through Rich's global `console` and many of them
call methods on the Textual app (`tui/app.py`), which is why a Rust frontend
cannot call them directly. :class:`UIPort` is the smallest interface that lets a
handler stay itself while its output travels somewhere else.

Two rules keep this honest:

* a command that is **not** ported answers :data:`NOT_PORTED_MESSAGE` with
  `ok=False` — never a silent no-op, because a UI that swallows input looks
  broken;
* the port never renders. It emits blocks; the client decides styling, matching
  the split `ui_events.py` already makes for the agent stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class Block:
    """One rendered block of command output.

    Attributes:
        kind: `"text"`, `"code"` or `"heading"`.
        text: The block's content, already plain (ANSI markup stripped).
    """

    kind: str
    text: str


@dataclass(frozen=True)
class ScreenSpec:
    """A modal the frontend should present.

    `kind` names a shape from a small, closed vocabulary rather than a widget, so
    one client renderer covers every screen a command can ask for instead of one
    widget per screen. The vocabulary is:

    * `"list"` — `payload["items"]`: `[{"id", "label", "detail"}]`, single select;
    * `"table"` — `payload["columns"]` (`[{"key", "title", "width"}]`) and
      `payload["rows"]` (`[{<column key>: text}]`), single select;
    * `"detail"` — `payload["markdown"]` plus `payload["actions"]`;
    * `"form"` — `payload["fields"]`, each `{"key", "label", "value"}` plus one of:
      `"toggle": true` (Space flips `"on"`/`"off"`, answers a boolean), `"options"`
      (a fixed list cycled with ←/→, answers the chosen string), or neither (free
      text, answers the typed string). Every field key appears in the answer;
    * `"markdown"` — `payload["markdown"]`, scrolling, no answer;
    * `"progress"` — `payload["label"]`, `payload["done"]`, `payload["total"]`;
    * `"confirm"` — `payload["message"]`, answered `True` or `False`;
    * `"split"` — `payload["left"]` and `payload["right"]` markdown panes plus
      `payload["actions"]`, answered as `{"action": "<id>"}`.

    The three human-in-the-loop interrupts keep their own kinds
    (`"tool_approval"`, `"question"`, `"plan"`) and arrive as `interrupt` frames
    rather than through a port, because they answer a running turn.

    Attributes:
        kind: One of the vocabulary above, or a command-specific tag.
        payload: JSON-safe body for the modal. Specs are built from the command's
            own data, so the Textual screen and the socket client read one
            source; the shared builders land with the screens wave.
        title: Optional heading.
    """

    kind: str
    payload: Any = None
    title: str = ""


@dataclass(frozen=True)
class CommandResult:
    """What a slash command produced, ready to become a `command_result` frame.

    Attributes:
        ok: Whether the command ran.
        message: One-line summary, or the reason it did not run.
        blocks: Output blocks in transcript order.
        action: Client-side follow-up (`"clear"`, `"quit"`), or `None`.
        prompt: Text to hand to the agent as a turn. Some handlers (`/init`,
            `/goal`, `/skill:x`) answer by returning a prompt instead of
            printing; the host starts that turn, exactly as the Textual UI's
            `_stream_prompt` does.
        screen: A modal the command wants shown, or `None`. Present when the
            command produced a spec but could not present it itself.
        attach: A session id the client should switch to, or `None`. The server
            names the target; only the connection's transport can move its sink.
        create: A session name the client should ask the server to spawn, or
            `None`. Spawning needs the connection, which a command does not own.
        resume: A saved session id the spawned child should continue, or `None`.
            Paired with `create`: the child starts with `--continue <id>`, so
            `@@/resume@@` restores a session without copying its messages.
        close: A session id to close, or `""` for the active one. `None` means no
            close was requested.
    """

    ok: bool
    message: str = ""
    blocks: tuple[Block, ...] = ()
    action: str | None = None
    prompt: str | None = None
    screen: ScreenSpec | None = None
    attach: str | None = None
    create: str | None = None
    resume: str | None = None
    close: str | None = None

    def as_blocks(self) -> list[dict[str, str]]:
        """Serialize the blocks as the protocol's kind/text dicts.

        Returns:
            A list of block dicts, in order.
        """
        return [{"kind": block.kind, "text": block.text} for block in self.blocks]


# The exact sentence an unported command answers with. Pinned by tests so a
# user never sees a command "succeed" by doing nothing.
NOT_PORTED_MESSAGE = "{name} is not ported to the ratatui UI yet"


def not_ported(name: str) -> CommandResult:
    """Build the explicit refusal for a command with no server-side port.

    Args:
        name: Command name without the leading `/`.

    Returns:
        A failed :class:`CommandResult` naming the command.
    """
    return CommandResult(ok=False, message=NOT_PORTED_MESSAGE.format(name=f"/{name}"))


@runtime_checkable
class UIPort(Protocol):
    """What a server-side command handler may do to the user's screen."""

    def log(self, block: Block) -> None:
        """Append one block to the transcript."""
        ...

    def notify(self, message: str, *, level: str = "info") -> None:
        """Show a transient notification, never transcript content."""
        ...

    def push_screen(self, spec: ScreenSpec) -> None:
        """Ask the frontend to present a modal."""
        ...

    async def ask(self, spec: ScreenSpec) -> Any:  # noqa: ANN401 — a modal returns its own shape
        """Present a modal and wait for its answer."""
        ...

    def set_status(self, key: str, value: str) -> None:
        """Update one status-bar field."""
        ...


def blocks_from_text(text: str) -> tuple[Block, ...]:
    """Split captured console output into transcript blocks.

    A fenced or boxed section (box-drawing characters, a fence line, or an
    indented run) becomes a code block; everything else stays text. Blank
    runs are collapsed so a command does not leave a hole in the transcript.

    Args:
        text: Plain output captured from the Rich console.

    Returns:
        The blocks, in order. Empty input yields no blocks.
    """
    blocks: list[Block] = []
    buffer: list[str] = []
    fence = chr(96) * 3

    def flush() -> None:
        if buffer:
            chunk = "\n".join(buffer).strip("\n")
            if chunk.strip():
                blocks.append(Block("text", chunk))
            buffer.clear()

    for line in text.splitlines():
        stripped = line.strip()
        boxed = stripped.startswith(("│", "┌", "└", "├", "╭", "╰", fence))
        if boxed or line.startswith("    "):
            flush()
            blocks.append(Block("code", line.rstrip()))
        elif not stripped:
            buffer.append("")
        else:
            buffer.append(line.rstrip())
    flush()
    return tuple(blocks)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from captured output.

    The Rust client cannot interpret terminal escapes and the protocol carries
    plain text, so leaving them in would render as visible noise.

    Args:
        text: Captured output, possibly containing escapes.

    Returns:
        The same text with escape sequences removed.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\x1b":
            out.append(char)
            index += 1
            continue
        index += 1
        if index < length and text[index] == "[":
            index += 1
            while index < length and not ("@" <= text[index] <= "~"):
                index += 1
            index += 1
        elif index < length:
            index += 1
    return "".join(out)


def merge_lines(lines: Sequence[str]) -> str:
    """Join captured lines, dropping trailing whitespace per line.

    Args:
        lines: Captured output lines.

    Returns:
        Newline-joined text with right-trimmed lines.
    """
    return "\n".join(line.rstrip() for line in lines)
