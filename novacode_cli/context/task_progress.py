"""Evidence-backed task progress carried by both compaction paths."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from novacode_cli.core.streaming import is_internal_context_text

if TYPE_CHECKING:
    from collections.abc import Sequence


MAX_ACTIONS = 4
MAX_TEXT = 120


def _short(value: Any) -> str:  # noqa: ANN401 - tool arguments have arbitrary schemas
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())[:MAX_TEXT]


def _prior(messages: Sequence[BaseMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            record = message.additional_kwargs.get("nova_task_progress")
            if isinstance(record, dict) and record.get("version") == 1:
                return record
    return {}


def task_progress(
    messages: Sequence[BaseMessage],
    todos: object = None,
    *,
    completed_head: int | None = None,
) -> dict[str, Any]:
    """Record observed actions and agent-reported todo state for the latest request.

    ``completed_head`` limits tool evidence to messages included in the summary.
    A verbatim tail remains authoritative and needs no duplicate action record.
    """
    prior = _prior(messages)
    latest_request = -1
    task = prior.get("task", "unknown")
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) and not message.additional_kwargs.get("lc_source"):
            content = message.content
            value = content if isinstance(content, str) else str(content)
            if value and not is_internal_context_text(value):
                latest_request, task = index, _short(value)

    same_task = latest_request < 0
    actions = list(prior.get("completed_actions", [])) if same_task else []
    prior_action_count = len(actions)
    seen = {item.get("id") for item in actions if isinstance(item, dict)}
    end = len(messages) if completed_head is None else completed_head
    results = {
        message.tool_call_id: message
        for message in messages[:end]
        if isinstance(message, ToolMessage)
    }
    todo_written = False
    for message in messages[max(0, latest_request + 1) : end]:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            if call.get("name") == "write_todos":
                todo_written = True
            call_id = call.get("id")
            result = results.get(call_id)
            if result is None or call_id in seen or call.get("name") == "write_todos":
                continue
            actions.append(
                {
                    "id": _short(call_id),
                    "tool": _short(call.get("name", "unknown")),
                    "args": _short(call.get("args") or {}),
                    "outcome": "error" if result.status == "error" else "returned",
                    "result": _short(result.content),
                }
            )
            seen.add(call_id)

    new_action = len(actions) > prior_action_count
    valid_todos = isinstance(todos, list) and (todo_written or (same_task and not new_action))
    if valid_todos:
        reported = [
            _short(item.get("content", ""))
            for item in todos
            if isinstance(item, dict) and item.get("status") == "completed"
        ]
        pending = next(
            (
                _short(item.get("content", ""))
                for item in todos
                if isinstance(item, dict) and item.get("status") in {"pending", "in_progress"}
            ),
            "unknown",
        )
    elif same_task:
        reported = list(prior.get("reported_completed_todos", []))
        pending = "unknown" if new_action else prior.get("next_action", "unknown")
    else:
        reported, pending = [], "unknown"

    return {
        "version": 1,
        "task": task,
        "completed_actions": actions[-MAX_ACTIONS:],
        "reported_completed_todos": reported[-MAX_ACTIONS:],
        "next_action": pending,
    }


def render_task_progress(record: dict[str, Any]) -> str:
    """Expose the structured record to the model without upgrading claims."""
    return "## Task progress (observed tool results; todos are agent-reported)\n" + json.dumps(
        record, ensure_ascii=False, separators=(",", ":")
    )
