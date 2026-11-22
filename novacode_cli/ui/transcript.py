"""Turn a checkpointed conversation back into transcript blocks.

A rewind (or a resume) replaces what the client shows, and the protocol has no
"restore this conversation" frame: a `command_result` may clear the transcript and
append blocks, so the kept messages are sent as blocks. That keeps the wire
untouched and the client dumb, at the cost of the per-message chrome the Textual
scrollback draws — role labels carry the same information.
"""

from __future__ import annotations

import json
from typing import Any

from novacode_cli.ui.port import Block

#: How much of one tool result or argument set a single block carries.
PREVIEW_CHARS = 400


def _text(content: Any) -> str:  # noqa: ANN401 — message content is model-shaped
    """Flatten a message body to text.

    Args:
        content: A string, or the list of typed parts a provider may return.

    Returns:
        The joined text, without markup.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text", "")))
        return "\n".join(part for part in parts if part).strip()
    return "" if content is None else str(content).strip()


def _clip(text: str) -> str:
    """Shorten *text* to :data:`PREVIEW_CHARS`, marking what was dropped."""
    flat = " ".join(text.split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return f"{flat[:PREVIEW_CHARS]}…"


def _tool_lines(message: Any) -> list[str]:  # noqa: ANN401 — a langchain AIMessage
    """Describe the calls one assistant message made, one line each."""
    lines = []
    for call in getattr(message, "tool_calls", None) or ():
        name = str(call.get("name", "tool"))
        args = call.get("args") or {}
        rendered = json.dumps(args, ensure_ascii=False, default=str) if args else ""
        lines.append(f"→ {name}({_clip(rendered)})")
    return lines


def message_blocks(messages: list[Any]) -> tuple[Block, ...]:
    """Render *messages* as the blocks that rebuild a transcript.

    Args:
        messages: LangChain messages, as the checkpoint holds them.

    Returns:
        One block per visible message; system prompts and empty bodies are
        skipped, and a tool result is clipped to :data:`PREVIEW_CHARS`.
    """
    blocks: list[Block] = []
    for message in messages:
        kind = type(message).__name__
        if kind in ("SystemMessage", "RemoveMessage"):
            continue
        if kind == "HumanMessage":
            body = _text(getattr(message, "content", ""))
            if body:
                blocks.append(Block("heading", "you"))
                blocks.append(Block("text", body))
            continue
        if kind == "ToolMessage":
            body = _text(getattr(message, "content", ""))
            if body:
                blocks.append(Block("code", _clip(body)))
            continue
        body = _text(getattr(message, "content", ""))
        if body:
            blocks.append(Block("heading", "nova"))
            blocks.append(Block("text", body))
        blocks.extend(Block("text", line) for line in _tool_lines(message))
    return tuple(blocks)
