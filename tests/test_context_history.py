"""Context estimates must exclude archived messages after compaction."""

import pytest

from novacode_cli.context.history import effective_messages


@pytest.mark.parametrize(("cutoff", "expected"), [(2, ["summary", "tail"]), (10, ["summary"])])
def test_compacted_context(cutoff: int, expected: list[str]) -> None:
    values = {
        "messages": ["old", "old", "tail"],
        "_summarization_event": {"cutoff_index": cutoff, "summary_message": "summary"},
    }
    assert effective_messages(values) == expected
    assert values["messages"] == ["old", "old", "tail"]


@pytest.mark.parametrize(
    "event", [None, {}, {"cutoff_index": 2}, {"cutoff_index": -1, "summary_message": "summary"}]
)
def test_no_valid_compaction_retains_history(event: object) -> None:
    values = {"messages": ["message"], "_summarization_event": event}
    assert effective_messages(values) == ["message"]
