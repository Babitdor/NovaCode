"""Request-local references for byte-identical successful file observations.

Tools always execute. Only duplicate text in the final dispatch view changes;
checkpoint messages, call arguments, ordering and full recovery stay intact.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage

MIN_OBSERVATION_CHARS = 2_048


def compact_observations(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Reference an earlier identical read in this view, never a cached read.

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
