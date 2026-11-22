"""Wire frames for the TUI server: typed builders over the shared protocol.

``novacode_cli.sessions.protocol`` stays the single encoder for `ui_events`; this
module only adds the envelopes a frontend needs to *drive* a session over a
socket (handshake, commands, notices). The purpose is that adding a frontend must
never fork event encoding: :func:`event_frame` delegates to
:func:`protocol.encode_event`, so a new `ui_events` dataclass is carried for
free.

Every existing envelope (`prompt`, `cancel`, `interrupt_reply`, `ev`,
`interrupt`, `turn_done`, `error`, `ready`) keeps the exact shape
`sessions/worker.py` already emits, so the stdio child, the parent supervisor,
headless mode and the Cowork app are untouched. See `rust/PROTOCOL.md`.

Why a byte cap here as well as in the child: one `ToolResult` can be megabytes,
and `asyncio`'s default `StreamReader` limit (64 KiB) would raise
`LimitOverrunError` and sever the session mid-turn — the reason
`sessions/supervisor.py` raises its own limit to the same 8 MiB.
"""

from __future__ import annotations

import json
from typing import Any

from novacode_cli.sessions import protocol

# Bumped only for an incompatible change; additive frames do not bump it.
PROTOCOL_VERSION = 1

# Longest accepted line, mirroring `sessions.supervisor._STREAM_LIMIT`.
MAX_LINE = 8 * 1024 * 1024

# Levels a `notice` frame may carry; the client styles them differently.
NOTICE_LEVELS = ("info", "warning", "error")


def hello(*, role: str, version: int = PROTOCOL_VERSION, name: str = "") -> dict[str, Any]:
    """Build the handshake frame both directions exchange.

    Args:
        role: `"server"` or `"client"`, which decides the field name.
        version: Protocol version this process speaks.
        name: Product/version string for diagnostics, e.g. `"nova-tui/0.1"`.

    Returns:
        The `hello` envelope.
    """
    frame: dict[str, Any] = {"t": "hello", "protocol": version}
    if name:
        frame[role] = name
    return frame


def ready(**fields: Any) -> dict[str, Any]:
    """Build the `ready` frame that ends the handshake.

    Args:
        **fields: Session facts (`session_id`, `cwd`, `model`, `assistant_id`,
            `ui`). Unknown keys are passed through for forward compatibility.

    Returns:
        The `ready` envelope with the protocol version filled in.
    """
    return {"t": "ready", "protocol": PROTOCOL_VERSION, **fields}


def error(message: str, *, code: str | None = None) -> dict[str, Any]:
    """Build a fatal or turn-level `error` frame.

    Args:
        message: Human-readable reason, already formatted for display.
        code: Optional machine-readable tag (`"auth"`, `"framing"`, ...).

    Returns:
        The `error` envelope.
    """
    frame: dict[str, Any] = {"t": "error", "message": message}
    if code:
        frame["code"] = code
    return frame


def notice(message: str, *, level: str = "info") -> dict[str, Any]:
    """Build a transient `notice` frame.

    Notices are for status the user should see but that is not transcript
    content, matching the TUI's `notify` rule.

    Args:
        message: Text to show.
        level: One of :data:`NOTICE_LEVELS`.

    Returns:
        The `notice` envelope.
    """
    return {"t": "notice", "level": level if level in NOTICE_LEVELS else "info", "message": message}


def ui_theme(
    name: str,
    *,
    dark: bool,
    primary: str = "",
    background: str = "",
    foreground: str = "",
    surface: str = "",
    panel: str = "",
    muted: str = "",
    border: str = "",
    success: str = "",
    warning: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Build a `ui_theme` frame: the palette the client should draw with.

    A theme is a client-side fact, so it travels as its own frame rather than
    inside a command result: the command still reports what it did, and the
    frame tells the client which colours to use from now on.

    Args:
        name: Theme name, as listed by `/theme`.
        dark: Whether the theme is dark.
        primary: Accent colour, `#rrggbb`.
        background: Page background, `#rrggbb`.
        foreground: Base text colour, `#rrggbb`.
        surface: Raised surface colour, `#rrggbb`.
        panel: Panel colour, `#rrggbb`.
        muted: Secondary text colour, empty when the theme defines none.
        border: Border colour, empty when the theme defines none.
        success: Success colour, for tool results.
        warning: Warning colour, for notices.
        error: Error colour, for failures.

    Returns:
        The `ui_theme` envelope, with empty colours omitted.
    """
    colours = {
        "primary": primary,
        "background": background,
        "foreground": foreground,
        "surface": surface,
        "panel": panel,
        "muted": muted,
        "border": border,
        "success": success,
        "warning": warning,
        "error": error,
    }
    frame: dict[str, Any] = {"t": "ui_theme", "name": name, "dark": dark}
    frame.update({key: value for key, value in colours.items() if value})
    return frame


def command_result(
    request_id: str,
    *,
    ok: bool,
    message: str = "",
    blocks: list[dict[str, str]] | None = None,
    action: str | None = None,
    prompt: str | None = None,
    screen: dict[str, Any] | None = None,
    attach: str | None = None,
    create: str | None = None,
    resume: str | None = None,
    close: str | None = None,
) -> dict[str, Any]:
    """Build the reply to a client `command` frame.

    Args:
        request_id: The `id` of the `command` frame being answered.
        ok: Whether the command ran. `False` must carry `message`.
        message: One-line summary, or the reason it failed.
        blocks: Rendered output, as `{"kind", "text"}` dicts.
        action: Client-side follow-up (`"clear"`, `"quit"`), or `None`.
        prompt: Text the command wants run as an agent turn (`/init`, `/goal`,
            `/skill:x` answer this way instead of printing). Additive; omitted
            when absent, so an older client sees the frame it always did.
        screen: A modal the command produced, as `{"kind", "title", "payload"}`.
            Additive, like `prompt`.
        attach: A session id the client should attach to (`/tabs`). Additive:
            only the connection's transport can move its sink, so the server
            names the target and the client sends the existing `attach` frame.
        create: A session name the client should spawn (`/tabs new`).
        resume: A saved session id the spawned child should continue
            (`/resume`). Additive, and only meaningful with `create`.
        close: A session id to close, or `""` for the active session.

    Returns:
        The `command_result` envelope.
    """
    frame: dict[str, Any] = {
        "t": "command_result",
        "id": request_id,
        "ok": ok,
        "message": message,
        "blocks": blocks or [],
    }
    if action:
        frame["action"] = action
    if prompt:
        frame["prompt"] = prompt
    if screen:
        frame["screen"] = screen
    if attach:
        frame["attach"] = attach
    if create:
        frame["create"] = create
    if resume:
        frame["resume"] = resume
    if close is not None:
        frame["close"] = close
    return frame


def screen(
    screen_id: str,
    *,
    kind: str,
    title: str = "",
    payload: Any = None,  # noqa: ANN401 — a screen body is command-specific JSON
) -> dict[str, Any]:
    """Build a `screen` frame: the server asks the client to present a modal.

    The payload is a shape from the vocabulary in
    `novacode_cli/ui/port.ScreenSpec`, never a widget description, so one client
    renderer covers every screen and the answer returns as plain JSON.

    Args:
        screen_id: Server-chosen id the matching `screen_result` must quote.
        kind: Screen kind, e.g. `"list"`, `"table"`, `"form"`, `"confirm"`.
        title: Heading to show above the body.
        payload: JSON-safe body for the screen.

    Returns:
        The `screen` envelope.
    """
    frame: dict[str, Any] = {"t": "screen", "id": screen_id, "kind": kind, "payload": payload}
    if title:
        frame["title"] = title
    return frame


def screen_result(screen_id: str, value: Any) -> dict[str, Any]:  # noqa: ANN401 — a modal owns its shape
    """Build a `screen_result` frame: the client answers a `screen`.

    Args:
        screen_id: The `id` of the `screen` frame being answered.
        value: The answer, in the shape that screen kind documents.

    Returns:
        The `screen_result` envelope.
    """
    return {"t": "screen_result", "id": screen_id, "result": value}


def screen_answer(msg: dict[str, Any]) -> tuple[str, Any] | None:
    """Decode a client `screen_result` frame.

    Args:
        msg: Parsed client envelope.

    Returns:
        `(screen_id, result)`, or `None` when the frame is not a usable answer.
    """
    if msg.get("t") != "screen_result":
        return None
    screen_id = msg.get("id")
    if not isinstance(screen_id, str) or not screen_id:
        return None
    return screen_id, msg.get("result")


def catalogue(commands: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a `catalogue` frame: every command the client may offer.

    Args:
        commands: One dict per command (`name`, `help`, `aliases`,
            `wants_text`, `route`, `served`), in `/help` order.

    Returns:
        The `catalogue` envelope.
    """
    return {"t": "catalogue", "commands": commands}


def create_session(
    *, name: str = "session", cwd: str | None = None, resume: str | None = None
) -> dict[str, Any]:
    """Ask the server to spawn a child session and attach this connection.

    Args:
        name: Display name for the new tab.
        cwd: Workspace to start in; omitted means the spawning session's cwd.
        resume: Saved session id to continue; the child starts with
            ``--continue <id>``, which restores that session's own messages,
            model and todos. Additive.

    Returns:
        The `create_session` envelope.
    """
    frame: dict[str, Any] = {"t": "create_session", "name": name}
    if cwd:
        frame["cwd"] = cwd
    if resume:
        frame["resume"] = resume
    return frame


def close_session(session_id: str) -> dict[str, Any]:
    """Ask the server to stop a child session this connection spawned.

    Args:
        session_id: Child to close.

    Returns:
        The `close_session` envelope.
    """
    return {"t": "close_session", "session_id": session_id}


def sessions_frame(sessions: list[dict[str, Any]], *, active: str | None) -> dict[str, Any]:
    """Build the reply to a client `sessions` frame.

    Args:
        sessions: One dict per session (`id`, `name`, `cwd`, `status`, `model`).
        active: The session the connection is attached to.

    Returns:
        The `sessions` envelope.
    """
    return {"t": "sessions", "sessions": sessions, "active": active}


def event_frame(event: Any, *, prompt_id: str | None = None) -> dict[str, Any] | None:  # noqa: ANN401
    """Encode one `ui_events` dataclass as an `ev` frame.

    Args:
        event: A `ui_events` dataclass instance.
        prompt_id: The prompt this event belongs to, when it has one.

    Returns:
        The `ev` envelope, or `None` when the event cannot be encoded (a
        non-event object), which callers skip rather than fail the turn on.
    """
    try:
        encoded = protocol.encode_event(event)
    except ValueError:
        return None
    frame: dict[str, Any] = {"t": "ev", **encoded}
    if prompt_id is not None:
        frame["prompt_id"] = prompt_id
    return frame


def interrupt_frame(request_id: str, *, kind: str, payload: Any) -> dict[str, Any]:  # noqa: ANN401
    """Build an `interrupt` frame for a human-in-the-loop request.

    `InterruptRequest` carries an `asyncio.Future` and is never encoded as an
    event; this envelope is how it crosses the socket.

    Args:
        request_id: Server-chosen id the reply must quote.
        kind: `"tool"`, `"question"` or `"plan"`.
        payload: JSON-safe request body.

    Returns:
        The `interrupt` envelope.
    """
    return {"t": "interrupt", "id": request_id, "kind": kind, "payload": payload}


def turn_done(prompt_id: str, *, ok: bool, steered: bool | None = None) -> dict[str, Any]:
    """Build a `turn_done` frame.

    Every accepted prompt gets exactly one, including when the turn is cancelled.

    Args:
        prompt_id: The client's prompt id.
        ok: `False` when the turn failed or was cancelled.
        steered: Set when a live follow-up was folded into a running turn.

    Returns:
        The `turn_done` envelope.
    """
    frame: dict[str, Any] = {"t": "turn_done", "id": prompt_id, "ok": ok}
    if steered is not None:
        frame["steered"] = steered
    return frame


def dumps(msg: dict[str, Any]) -> str:
    """Serialize *msg* as one newline-terminated JSON line.

    Args:
        msg: Envelope to send.

    Returns:
        The line, ready to write to the socket.
    """
    return protocol.dumps(msg)


def decode_line(raw: bytes | str) -> dict[str, Any] | None:
    """Parse one received line into an envelope.

    Args:
        raw: One line of bytes or text read from the socket.

    Returns:
        The parsed envelope, or `None` for blank lines, non-JSON noise and JSON
        that is not an object. Mirrors :func:`protocol.loads`.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return protocol.loads(text)


def encode_line(msg: dict[str, Any]) -> bytes:
    """Serialize *msg* for the wire, rejecting anything over :data:`MAX_LINE`.

    Args:
        msg: Envelope to send.

    Returns:
        UTF-8 bytes, newline-terminated.

    Raises:
        FrameError: When the encoded frame exceeds the line cap.
    """
    data = dumps(msg).encode("utf-8")
    if len(data) > MAX_LINE:
        msg_text = f"frame of {len(data)} bytes exceeds the {MAX_LINE}-byte line limit"
        raise FrameError(msg_text)
    return data


class FrameError(ValueError):
    """A frame cannot be put on the wire (oversized) or parsed (oversized)."""


def validate_client_frame(msg: dict[str, Any], *, authed: bool) -> str | None:
    """Check one client frame against the handshake state.

    Args:
        msg: Parsed client envelope.
        authed: Whether the connection has completed `auth`.

    Returns:
        An error message when the frame must close the connection, else `None`.
    """
    kind = msg.get("t")
    if not isinstance(kind, str) or not kind:
        return "frame is missing a type"
    if not authed and kind not in {"hello", "auth"}:
        return "expected hello or auth before ready"
    if authed and kind in {"hello", "auth"}:
        return "already authenticated"
    return None


def json_size(msg: dict[str, Any]) -> int:
    """Return the encoded size of *msg* in bytes, for bounding tests and logs.

    Args:
        msg: Envelope to measure.

    Returns:
        Byte length of the encoded line.
    """
    return len(json.dumps(msg, ensure_ascii=False, default=str).encode("utf-8"))
