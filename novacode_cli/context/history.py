"""Reconstruct the conversation sent to the model after library compaction."""

from collections.abc import Mapping
from typing import Any


def effective_messages(values: Mapping[str, Any]) -> list[Any]:
    """Use the summary and retained tail instead of the checkpoint archive."""
    messages = list(values.get("messages", []))
    event = values.get("_summarization_event")
    if not isinstance(event, Mapping):
        return messages
    cutoff = event.get("cutoff_index")
    summary = event.get("summary_message")
    if not isinstance(cutoff, int) or cutoff < 0 or summary is None:
        return messages
    return [summary, *messages[cutoff:]]
