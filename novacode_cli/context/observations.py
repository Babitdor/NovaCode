"""Request-local compaction of redundant successful tool observations.

Tools always execute. Only redundant text in the final dispatch view changes;
checkpoint messages, call arguments, ordering and full recovery stay intact.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage

MIN_OBSERVATION_CHARS = 2_048


def _compact_code_result(message: ToolMessage) -> ToolMessage:
    """Keep selected data and ordered operation identities, with success implicit."""
    if message.name not in {"eval", "MCPCode"} or message.status == "error":
        return message
    if not isinstance(message.content, str):
        return message
    try:
        body = json.loads(message.content)
    except ValueError:
        return message
    # Only the complete, known success envelope qualifies; future fields stay visible.
    if not isinstance(body, dict) or set(body) != {
        "result",
        "truncated",
        "calls",
        "failed_calls",
        "outcomes",
        "outcomes_omitted",
        "recovery",
    }:
        return message
    outcomes = body["outcomes"]
    if (
        body["truncated"] is not False
        or body["failed_calls"] != 0
        or body["outcomes_omitted"] != 0
        or not isinstance(outcomes, list)
        or body["calls"] != len(outcomes)
        or body["recovery"]
        != ("Do not replay completed calls. Failed/cancelled calls may have side effects.")
        or any(
            not isinstance(step, dict)
            or step.get("step") != index
            or step.get("status") != "completed"
            or "error" in step
            for index, step in enumerate(outcomes, 1)
        )
    ):
        return message
    compact = {
        "result": body["result"],
        "completed": [
            {key: value for key, value in step.items() if key not in {"step", "status"}}
            for step in outcomes
        ],
    }
    return message.model_copy(update={"content": json.dumps(compact, ensure_ascii=False)})


def compact_observations(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Compact success bookkeeping and reference identical reads in this view.

    Keys include the exact arguments and text, rather than just the file path.
    A read after an edit must have actually returned the same bytes to qualify.
    Sources must survive in the same request; rebuilding from original messages
    on every dispatch prevents dangling references after compaction or resume.
    Nontext results and failed reads are never rewritten.
    """
    result = list(messages)
    pending: dict[str, str] = {}
    seen: dict[tuple[str, str], int] = {}
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = call.get("id")
                if call_id:
                    pending.pop(call_id, None)
                    if call["name"] == "read_file":
                        pending[call_id] = json.dumps(call["args"], sort_keys=True)
        elif isinstance(message, ToolMessage):
            result[index] = _compact_code_result(message)
            arguments = pending.pop(message.tool_call_id, None)
            if (
                arguments is None
                or message.name != "read_file"
                or message.status == "error"
                or not isinstance(message.content, str)
                or len(message.content) < MIN_OBSERVATION_CHARS
            ):
                continue
            key = (arguments, message.content)
            if key not in seen:
                seen[key] = index
                continue
            source = seen[key] + 1
            result[index] = message.model_copy(
                update={
                    "content": (
                        "[Identical read_file output: this fresh read returned exactly the text "
                        f"retained in request message {source} "
                        "(counting conversation messages from 1, excluding the system prompt). "
                        "Use that full result; no new contents omitted.]"
                    )
                }
            )
    return result
