"""A compaction summary is a DERIVED artifact and must be labelled as one.

``compact_conversation`` rewrites the conversation into a single summary and
persists that summary as a durable ``session-summary`` lesson, which is then
re-injected into future prompts. Unlabelled, a lossy summary is indistinguishable
from verified fact after a resume — the model has no reason to doubt it.

These drive the real ``compact_conversation`` (with the LLM call stubbed) and
assert the label lands on disk.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli import compaction


class _Agent:
    """Minimal agent surface ``compact_conversation`` touches."""

    def __init__(self) -> None:
        self.updated: dict | None = None

    async def aget_state(self, config):  # noqa: ANN001, ANN201, ARG002
        return SimpleNamespace(values={"messages": [HumanMessage(content="do the thing")]})

    async def aupdate_state(self, **kwargs):  # noqa: ANN003, ANN201
        self.updated = kwargs


class _Model:
    def get_num_tokens_from_messages(self, _messages) -> int:  # noqa: ANN001
        return 42


def _model() -> Any:
    """A BaseChatModel stand-in. Typed Any so it can be passed where the real
    type is expected — only two methods are ever touched."""
    return _Model()


@pytest.fixture()
def stub_summarizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the LLM summarization with a deterministic string."""

    async def _fake(model, messages, focus_instructions=None, context_window=None, **_kw):  # noqa: ANN001, ANN202, ARG001
        return "We fixed the glob bug and shipped it."

    monkeypatch.setattr(compaction, "summarize_conversation", _fake)


@pytest.mark.asyncio
async def test_derived_marker_is_written_to_the_lesson_file(
    tmp_path, stub_summarizer: None
) -> None:
    """End-to-end: the label reaches the file the middleware re-injects."""
    result = await compaction.compact_conversation(
        _Agent(), _model(), "thread-1", agent_dir=tmp_path
    )

    assert result.success
    lesson_file = tmp_path / "memories" / "session-summary.md"
    assert lesson_file.exists(), "the session summary was not persisted as a lesson"

    written = lesson_file.read_text(encoding="utf-8")
    assert "derived" in written.lower(), "the summary was stored as unlabelled fact"
    assert "not a verified record" in written
    # The actual summary content must still be there — the label is additive.
    assert "We fixed the glob bug" in written


@pytest.mark.asyncio
async def test_lesson_index_points_at_the_summary_topic(
    tmp_path, stub_summarizer: None
) -> None:
    """The topic must be registered, or the lesson never reaches the model."""
    await compaction.compact_conversation(_Agent(), _model(), "thread-1", agent_dir=tmp_path)

    index = (tmp_path / "memories" / "INDEX.md").read_text(encoding="utf-8")
    assert "session-summary" in index


@pytest.mark.asyncio
async def test_compaction_without_agent_dir_does_not_persist(
    tmp_path, stub_summarizer: None
) -> None:
    """No agent_dir -> no lesson written, and no crash."""
    result = await compaction.compact_conversation(_Agent(), _model(), "thread-1", agent_dir=None)

    assert result.success
    assert result.learnings == ""
    assert not (tmp_path / "memories").exists()


@pytest.mark.asyncio
async def test_summary_is_still_returned_unlabelled_for_the_transcript(
    stub_summarizer: None,
) -> None:
    """The label belongs to the durable artifact, not the user-facing summary.

    ``CompactionResult.summary`` feeds the transcript/UI; prefixing it there
    would surface prompt plumbing to the user.
    """
    result = await compaction.compact_conversation(_Agent(), _model(), "thread-1")

    assert result.success
    assert result.summary == "We fixed the glob bug and shipped it."
    assert "derived" not in result.summary.lower()
