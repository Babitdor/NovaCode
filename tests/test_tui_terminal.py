"""`!!cmd` in the socket server: a real TTY, streamed to the client as text.

The pane is plain lines rather than a cell grid, so these tests pin the shaping
rules (colour removed, a carriage return rewrites its line, a partial last line
waits for its newline) and the frame round trip: output goes out as `pty`
frames, keys come back as `pty_input`, and the pane closes with `pty_exit`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from novacode_cli.server.host import SessionHost, SessionSpec
from novacode_cli.ui.terminal import MAX_LINES, Terminal

if TYPE_CHECKING:
    import pytest

MAX_WAIT = 5.0


class _SS:
    """Minimal session-state stand-in."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos = None
        self.steering_instructions = []
        self.headless_out_fd = None


class _Shell:
    """A pseudo-terminal that yields scripted output."""

    def __init__(self, output: list[str]) -> None:
        self.output = output
        self.typed: list[str] = []
        self.terminated = False
        self.closed = False

    async def read(self) -> str | None:
        """Return the next scripted chunk, then EOF."""
        return self.output.pop(0) if self.output else ""

    async def wait(self) -> int:
        """The child always exits cleanly here."""
        return 0

    def write(self, text: str) -> None:
        """Record keys sent to the child."""
        self.typed.append(text)

    def terminate(self) -> None:
        """Record the stop."""
        self.terminated = True

    def close(self) -> None:
        """Record the release."""
        self.closed = True

def _patch_spawn(
    monkeypatch: pytest.MonkeyPatch,
    shell: _Shell,
    seen: list[tuple[str, object]],
) -> None:
    """Make `Terminal.spawn` hand back a scripted shell."""
    import novacode_cli.ui.terminal as terminal_module

    async def spawn(_cls: object, command: str, cwd: object = "", **_kwargs: object) -> Terminal:
        seen.append((command, cwd))
        return Terminal(shell, command)

    monkeypatch.setattr(terminal_module.Terminal, "spawn", classmethod(spawn))


def _spec() -> SessionSpec:
    """A session spec that needs no agent."""
    return SessionSpec(
        agent=object(),
        session_state=_SS(),
        assistant_id="nova-agent",
        model_name="test-model",
        cwd="C:/work",
    )


async def _host() -> tuple[SessionHost, list[dict[str, Any]]]:
    """A started host whose frames are captured."""
    sent: list[dict[str, Any]] = []
    host = SessionHost(sent.append, _spec)
    await host.start()
    sent.clear()
    return host, sent


async def _wait_for(sent: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Wait for the next frame of *kind*."""
    for _ in range(int(MAX_WAIT / 0.01)):
        found = [frame for frame in sent if frame["t"] == kind]
        if found:
            return found[-1]
        await asyncio.sleep(0.01)
    message = f"no {kind} frame arrived"
    raise AssertionError(message)


async def _drain(terminal: Terminal) -> list[str]:
    """Read a whole scripted terminal to the end."""
    chunks: list[str] = []
    for _ in range(50):
        chunk = await terminal.drain()
        if chunk:
            chunks.append(chunk)
        if terminal.finished:
            break
    return chunks


# ── shaping ──────────────────────────────────────────────────────────────


async def test_colour_is_removed_before_the_client_sees_it() -> None:
    terminal = Terminal(_Shell(["\x1b[32mok\x1b[0m\n"]), "pytest")

    assert await _drain(terminal) == ["ok"]


async def test_a_carriage_return_rewrites_its_line() -> None:
    terminal = Terminal(_Shell(["50%\r100%\n"]), "make")

    assert await _drain(terminal) == ["100%"]
    assert terminal.output == "100%"


async def test_a_partial_last_line_waits_for_its_newline() -> None:
    terminal = Terminal(_Shell(["one\ntw"]), "cat")

    assert await _drain(terminal) == ["one", "tw"]
    assert terminal.output == "one\ntw"


async def test_backspace_removes_the_previous_character() -> None:
    terminal = Terminal(_Shell(["ab\bc\n"]), "read")

    assert await _drain(terminal) == ["ac"]


async def test_output_is_capped() -> None:
    lines = [f"line {index}\n" for index in range(MAX_LINES + 10)]
    terminal = Terminal(_Shell(["".join(lines)]), "yes")

    await _drain(terminal)

    assert len(terminal.output.splitlines()) == MAX_LINES


async def test_exit_status_is_reported_once() -> None:
    terminal = Terminal(_Shell([""]), "false")

    await _drain(terminal)

    assert terminal.finished is True
    assert terminal.exit_code == 0


async def test_stop_terminates_and_releases_the_child() -> None:
    shell = _Shell(["still running\n"])
    terminal = Terminal(shell, "sleep 30")

    await terminal.stop()

    assert shell.terminated is True
    assert shell.closed is True
    assert terminal.finished is True


async def test_keys_are_ignored_after_the_child_exits() -> None:
    shell = _Shell([""])
    terminal = Terminal(shell, "true")
    await _drain(terminal)

    terminal.write("x")

    assert shell.typed == []


async def test_keys_reach_the_child_while_it_runs() -> None:
    shell = _Shell([])
    terminal = Terminal(shell, "python")

    terminal.write("\r")
    terminal.resize(40, 120)

    assert shell.typed == ["\r"]


# ── the socket round trip ────────────────────────────────────────────────


async def test_a_bang_bang_prompt_opens_a_terminal_and_streams_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = _Shell(["C:/work\n", "done\n"])
    spawned: list[tuple[str, object]] = []
    _patch_spawn(monkeypatch, shell, spawned)
    host, sent = await _host()

    await host.prompt("p1", "!!pwd")
    opened = await _wait_for(sent, "pty")
    await _wait_for(sent, "pty_exit")

    assert spawned
    assert spawned[0][0] == "pwd"
    assert opened["command"] == "pwd"
    assert "C:/work" in opened["text"]
    exit_frame = next(frame for frame in sent if frame["t"] == "pty_exit")
    assert exit_frame["code"] == 0
    await host.close()


async def test_keys_and_close_reach_the_open_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Idle(_Shell):
        """A child that stays alive with no output, like an interactive prompt."""

        async def read(self) -> str | None:
            return None

    shell = _Idle([])
    _patch_spawn(monkeypatch, shell, [])
    host, _sent = await _host()
    await host.prompt("p1", "!!python")
    await asyncio.sleep(0.05)
    terminal_id = host._terminal_id
    assert terminal_id is not None

    host.terminal_keys("wrong-id", "nope")
    host.terminal_keys(terminal_id, "print(1)\r")
    await host.terminal_close(terminal_id)

    assert shell.typed == ["print(1)\r"]
    assert host._terminal_id is None
    await host.close()


async def test_a_second_terminal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Idle(_Shell):
        """A child that stays alive with no output."""

        async def read(self) -> str | None:
            return None

    _patch_spawn(monkeypatch, _Idle([]), [])
    host, sent = await _host()
    await host.prompt("p1", "!!python")

    await host.prompt("p2", "!!pwd")

    assert any(
        frame["t"] == "notice" and "already open" in frame["message"] for frame in sent
    )
    await host.close()


async def test_an_empty_bang_bang_prompt_explains_itself() -> None:
    host, sent = await _host()

    await host.prompt("p1", "!!")

    assert any(frame["t"] == "notice" and "Type a command" in frame["message"] for frame in sent)
    await host.close()
