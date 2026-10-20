"""Complete, independent handoff snapshots with explicit overflow failure."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

from novacode_cli.context.estimates import estimate_content
from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage


def conversation_snapshot(messages: list[AnyMessage], budget: int) -> tuple[list[AnyMessage], str]:
    """Keep completed pairs; never silently discard constraints to fit a budget."""
    completed = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    stop = len(messages)
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and any(
            call.get("id") not in completed for call in message.tool_calls
        ):
            stop = index
            break
    result = deepcopy(messages[:stop])
    if estimate_content(result).total > budget:
        msg = "Protected context overflow: compact the parent before spawning"
        raise TeamError(msg)
    encoded = json.dumps([message.model_dump(mode="json") for message in result], sort_keys=True)
    return result, hashlib.sha256(encoded.encode()).hexdigest()
