"""The two cheap context strategies that run before an automatic compaction.

Nova shrinks a long conversation in three steps, cheapest first:

1. **Snip** (:func:`snip_history`) drops zombie messages and stale markers:
   empty assistant turns, repeated copies of the same internal-context note, file
   reads superseded by a later read of the same range, and to-do lists replaced
   by a later one. Nothing the model still needs is lost: the newer copy stays.
2. **Collapse** (:func:`collapse_turns`) restructures completed turns before a
   boundary: each keeps the user's message and the turn's final answer verbatim,
   and its tool loop becomes one digest line per call, with every large result
   saved to ``/cleared/`` so ``read_file`` can bring it back.
3. **Auto-compact** (``agents/context_compaction.py``) summarizes with a model
   call, only if the first two were not enough.

Both are request-local views, never edits to state: checkpoints, ``/rewind``,
``/tree`` and resume see the full history. Both leave the current turn alone
(everything from the latest user message on), so a running agent's working set
is never edited mid-run, and they only change at a turn boundary, which keeps
the provider's prompt cache stable within a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.core.streaming import is_internal_context_text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from langchain_core.messages import AnyMessage

#: Completed turns kept verbatim at the end (the current one is always kept).
KEEP_TURNS = 3
#: Collapse once the request reaches this share of the window (auto-compact is ~0.82).
COLLAPSE_AT = 0.6
#: Results at least this long are saved to /cleared/ when collapsed.
OFFLOAD_CHARS = 400
#: Digest lines per collapsed turn before "… N more".
DIGEST_LINES = 40

SUMMARY_SOURCE = "summarization"


@dataclass
class StrategyReport:
    """What a strategy removed from the request, for /context."""

    messages: int = 0
    chars: int = 0
    turns: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, chars: int, messages: int = 0) -> None:
        """Count one snip or collapse."""
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        self.chars += max(0, chars)
        self.messages += messages


def _text(message: AnyMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def is_user_turn(message: AnyMessage) -> bool:
    """A message the user typed, not an injected note or a summary."""
    if not isinstance(message, HumanMessage):
        return False
    if (message.additional_kwargs or {}).get("lc_source"):
        return False
    return not is_internal_context_text(_text(message))


def current_turn_start(messages: Sequence[AnyMessage]) -> int:
    """Index of the latest user message; everything from here is the live turn."""
    for index in range(len(messages) - 1, -1, -1):
        if is_user_turn(messages[index]):
            return index
    return len(messages)


def _read_key(call: dict[str, Any]) -> str:
    args = call.get("args") or {}
    path = args.get("file_path") or args.get("path") or ""
    return json.dumps([path, args.get("offset"), args.get("limit")])


def snip_history(  # noqa: PLR0912 - one branch per kind of zombie
    messages: Sequence[AnyMessage],
) -> tuple[list[AnyMessage], StrategyReport]:
    """Drop zombies and stale markers from the history before the current turn."""
    report = StrategyReport()
    end = current_turn_start(messages)
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                calls[call.get("id") or ""] = call

    # Newest occurrence of each read range / internal note / to-do list wins.
    latest_read: dict[str, int] = {}
    latest_note: dict[str, int] = {}
    latest_todos = -1
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage) and message.status != "error":
            call = calls.get(message.tool_call_id) or {}
            if message.name == "read_file":
                latest_read[_read_key(call)] = index
            elif message.name == "write_todos":
                latest_todos = index
        elif isinstance(message, HumanMessage) and not is_user_turn(message):
            if (message.additional_kwargs or {}).get("lc_source") != SUMMARY_SOURCE:
                latest_note[_text(message)] = index

    out: list[AnyMessage] = []
    for index, message in enumerate(messages):
        if index >= end:
            out.append(message)
            continue
        if isinstance(message, AIMessage) and not message.tool_calls and not _text(message).strip():
            report.add("empty assistant message", 0, messages=1)
            continue
        if isinstance(message, HumanMessage) and not is_user_turn(message):
            text = _text(message)
            if latest_note.get(text, index) != index:
                report.add("repeated internal note", len(text), messages=1)
                continue
        if isinstance(message, ToolMessage) and message.status != "error":
            call = calls.get(message.tool_call_id) or {}
            if message.name == "read_file" and latest_read.get(_read_key(call), index) != index:
                path = (call.get("args") or {}).get("file_path") or "this file"
                stub = (
                    f"[Superseded read of {path}: a later read in this conversation "
                    "has the current contents.]"
                )
                report.add("superseded file read", len(_text(message)) - len(stub))
                out.append(message.model_copy(update={"content": stub}))
                continue
            if message.name == "write_todos" and index != latest_todos:
                stub = "[Superseded to-do list: a later write_todos holds the current one.]"
                report.add("superseded to-do list", len(_text(message)) - len(stub))
                out.append(message.model_copy(update={"content": stub}))
                continue
        out.append(message)
    return out, report


def _one_line(text: str, limit: int = 100) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _call_label(call: dict[str, Any]) -> str:
    args = call.get("args") or {}
    target = (
        args.get("file_path")
        or args.get("path")
        or args.get("command")
        or args.get("pattern")
        or args.get("query")
        or next((v for v in args.values() if isinstance(v, str)), "")
    )
    return f"{call.get('name', 'tool')} {_one_line(str(target), 80)}".strip()


def collapse_boundary(messages: Sequence[AnyMessage], keep_turns: int = KEEP_TURNS) -> str | None:
    """Id of the user message that starts the oldest kept turn, or None."""
    starts = [i for i, m in enumerate(messages) if is_user_turn(m)]
    if len(starts) <= keep_turns:
        return None
    return messages[starts[-keep_turns]].id


def collapse_turns(
    messages: Sequence[AnyMessage],
    boundary_id: str | None,
    save: Callable[[str, str | None, str], str | None] | None = None,
) -> tuple[list[AnyMessage], StrategyReport]:
    """Restructure the completed turns before ``boundary_id`` into digests.

    ``save(call_id, tool_name, text)`` stores a large result and returns the
    placeholder naming where it went (``None`` when it could not be saved).
    """
    report = StrategyReport()
    end = next((i for i, m in enumerate(messages) if boundary_id and m.id == boundary_id), None)
    if end is None:  # no boundary, or it was summarized away
        return list(messages), report
    end = min(end, current_turn_start(messages))

    out: list[AnyMessage] = []
    index = 0
    while index < end:
        message = messages[index]
        if not is_user_turn(message):
            out.append(message)
            index += 1
            continue
        stop = next((j for j in range(index + 1, end) if is_user_turn(messages[j])), end)
        out.extend(_collapse_turn(list(messages[index:stop]), save, report))
        index = stop
    out.extend(messages[end:])
    return out, report


def _collapse_turn(
    turn: list[AnyMessage],
    save: Callable[[str, str | None, str], str | None] | None,
    report: StrategyReport,
) -> list[AnyMessage]:
    """One completed turn: its user messages, one digest, its final answer."""
    loop = [
        m for m in turn if isinstance(m, ToolMessage) or (isinstance(m, AIMessage) and m.tool_calls)
    ]
    if not loop:
        return turn
    calls: dict[str, dict[str, Any]] = {}
    for message in loop:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                calls[call.get("id") or ""] = call
    lines: list[str] = []
    for message in loop:
        if isinstance(message, AIMessage):
            note = _text(message).strip()
            if note:
                lines.append(f"- note: {_one_line(note, 160)}")
            continue
        call = calls.get(message.tool_call_id) or {"name": message.name}
        text = _text(message)
        outcome = "failed" if message.status == "error" else f"{len(text.splitlines())} lines"
        where = (
            save(message.tool_call_id, message.name, text)
            if save and len(text) >= OFFLOAD_CHARS
            else None
        )
        tail = (
            f" (saved: {where})" if where else f": {_one_line(text, 120)}" if text.strip() else ""
        )
        lines.append(f"- {_call_label(call)} → {outcome}{tail}")
    shown = lines[:DIGEST_LINES]
    if len(lines) > DIGEST_LINES:
        shown.append(f"- … {len(lines) - DIGEST_LINES} more steps")
    digest = (
        f"[Collapsed earlier work: {sum(isinstance(m, ToolMessage) for m in loop)} tool calls. "
        "Saved results can be read back with read_file.]\n" + "\n".join(shown)
    )

    in_loop = {id(m) for m in loop}
    kept = [m for m in turn if id(m) not in in_loop]
    before = sum(
        len(_text(m)) + len(json.dumps(getattr(m, "tool_calls", None) or [])) for m in loop
    )
    answer = next((m for m in reversed(kept) if isinstance(m, AIMessage)), None)
    out: list[AnyMessage] = []
    for message in kept:
        if message is answer:
            # One assistant message: digest first, then the turn's answer.
            out.append(AIMessage(content=f"{digest}\n\n{_text(answer)}", id=answer.id))
        else:
            out.append(message)
    if answer is None:
        out.insert(1 if out else 0, AIMessage(content=digest))
    report.turns += 1
    report.add("collapsed turn", before - len(digest), messages=len(loop))
    return out
