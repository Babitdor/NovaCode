"""Skills are tiered, not dumped into every prompt.

The old SkillsMiddleware listed all ~630 skills (~200k chars) on every model
call. Now: a budgeted listing, per-turn suggestions, and ``skills_search``.
The embedder is replaced by a bag-of-words stand-in so these run offline and
deterministically.
"""

from __future__ import annotations

import re
import zlib

import numpy as np
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import novacode_cli.skills.retrieval as R
from novacode_cli.skills.refreshing_middleware import (
    SUGGESTION_MARKER,
    RefreshingSkillsMiddleware,
    SubagentSkillsMiddleware,
    listing_budget,
)


def _skill(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "path": f"/skills/{name}/SKILL.md",
        "license": None,
        "compatibility": None,
        "metadata": {},
        "allowed_tools": [],
    }


SKILLS = [
    _skill("pptx", "Create and edit PowerPoint pptx presentation slide decks."),
    _skill("docker-reviewer", "Review docker-compose files for security and correctness."),
    _skill("clickhouse-io", "ClickHouse database query optimization and analytics patterns."),
    *[
        _skill(f"filler-{i}", f"Unrelated filler skill number {i} about gardening.")
        for i in range(40)
    ],
]


class _BagOfWords:
    """Deterministic stand-in for the static embedder: hashed word counts."""

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), 512))
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z]+", text.lower()):
                out[row, zlib.crc32(word.encode()) % 512] += 1
        return out


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    model = _BagOfWords()
    monkeypatch.setattr(R, "_model", lambda: model)
    monkeypatch.setattr("novacode_cli.skills.refreshing_middleware.prewarm", lambda: None)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: set())
    R._index_cache.clear()


class _Loaded(RefreshingSkillsMiddleware):
    """Test seam: the skill list lives on the instance now (it used to arrive in
    a state update), so ``_finish(state, {"skills_metadata": ...})`` loads it."""

    def _finish(self, state, update=None):  # noqa: ANN001, ANN202
        if update is not None:
            self._skills = list(update["skills_metadata"])
        return super()._finish(state)


def _mw(listing_chars: int = 600) -> RefreshingSkillsMiddleware:
    return _Loaded(backend=None, sources=["/skills/"], listing_chars=listing_chars)


def test_listing_stays_within_budget_and_points_at_search() -> None:
    text = _mw(600)._format_skills_list(SKILLS)
    assert len(text) < 700
    assert "more skills: `skills_search` finds them" in text


def test_dense_search_can_return_every_match_beyond_twenty() -> None:
    items = [_skill(f"backup-{i}", "Encrypt stored backups securely.") for i in range(35)]
    results = R.get_index(items).search("encrypt backups", k=len(items))
    assert {item["name"] for item, _cosine, _bm25 in results} == {item["name"] for item in items}


def test_suggestion_and_repeated_search_reuse_the_query_embedding(monkeypatch):
    encoded = []

    class Model(_BagOfWords):
        def encode(self, texts):
            encoded.append(list(texts))
            return super().encode(texts)

    model = Model()
    monkeypatch.setattr(R, "_model", lambda: model)
    index = R.get_index(SKILLS)
    query = "review docker compose files for security and correctness"
    index.suggest(query)
    first = index.search(query)
    assert index.search(query) == first
    assert encoded[1:] == [[query]], "suggest/search must share one normalized query vector"
    assert all(len(key) == 64 for key in R._index_cache)


def test_index_cache_preserves_recent_inventories():
    inventories = [[_skill(f"skill-{i}", f"Description {i}")] for i in range(5)]
    first = R.get_index(inventories[0])
    second = R.get_index(inventories[1])
    R.get_index(inventories[2])
    R.get_index(inventories[3])
    assert R.get_index(inventories[0]) is first
    R.get_index(inventories[4])
    assert R.get_index(inventories[0]) is first
    assert R.get_index(inventories[1]) is not second


def test_shared_index_does_not_confuse_tool_and_subagent_metadata() -> None:
    tool = {"name": "reviewer", "description": "Review code.", "kind": "tool"}
    agent = {**tool, "kind": "subagent"}
    R.get_index([tool])
    results = R.get_index([agent]).search("review code", k=1)
    assert results[0][0]["kind"] == "subagent"


def test_snapshot_replacement_discards_derived_index(monkeypatch: pytest.MonkeyPatch) -> None:
    first = R.get_index(SKILLS)
    model = _BagOfWords()
    monkeypatch.setattr(R, "_model", lambda: model)
    replacement = R.get_index(SKILLS)
    assert replacement is not first
    assert replacement._model is model
    monkeypatch.setattr(R, "_model", lambda: None)
    assert not R.get_index(SKILLS).dense


async def test_async_suggestions_run_on_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from unittest.mock import AsyncMock

    mw = _mw()
    caller = threading.get_ident()
    finished = []
    monkeypatch.setattr(mw, "_read_usage", AsyncMock())
    monkeypatch.setattr(mw, "_load_skills", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mw, "_finish", lambda _state: finished.append(threading.get_ident()))
    monkeypatch.setattr(mw, "_pin_named_skills", lambda *_args: None)
    await mw.abefore_agent({}, None, {})
    assert len(finished) == 1
    assert finished[0] != caller


def test_listing_ranks_most_used_first() -> None:
    mw = _mw(200)
    mw._usage = {"clickhouse-io": 9}
    assert mw._format_skills_list(SKILLS).startswith("- **clickhouse-io**")


def test_listing_budget_scales_with_window_and_is_capped() -> None:
    assert listing_budget(0) == 4_000
    assert listing_budget(128_000) == 5_120
    assert listing_budget(1_000_000) == 8_000


def test_relevant_request_gets_a_suggestion_chit_chat_does_not() -> None:
    mw = _mw()
    note = mw._finish(
        {"messages": [HumanMessage("review my docker-compose files for security")]},
        {"skills_metadata": SKILLS},
    )
    assert note["messages"][0].content.startswith(SUGGESTION_MARKER)
    assert "docker-reviewer" in note["messages"][0].content
    assert "messages" not in (
        mw._finish({"messages": [HumanMessage("thanks")]}, {"skills_metadata": SKILLS}) or {}
    )


def test_no_suggestion_unless_the_newest_message_is_the_user() -> None:
    state = {"messages": [HumanMessage("optimize clickhouse database query"), AIMessage("ok")]}
    assert "messages" not in (_mw()._finish(state, {"skills_metadata": SKILLS}) or {})


def test_a_suggestion_can_recur_when_the_skill_was_not_loaded() -> None:
    mw, q = _mw(), "optimize my clickhouse database query analytics"
    first = mw._finish({"messages": [HumanMessage(q)]}, {"skills_metadata": SKILLS})["messages"][0]
    again = mw._finish(
        {"messages": [HumanMessage(q), first, AIMessage("done"), HumanMessage(q)]},
        {"skills_metadata": SKILLS},
    )
    assert "clickhouse-io" in str((again or {}).get("messages", ""))


def test_skill_search_returns_paths() -> None:
    mw = _mw()
    mw._finish({"messages": []}, {"skills_metadata": SKILLS})
    out = mw.tools[0].invoke({"query": "powerpoint slide deck"})
    assert "/skills/pptx/SKILL.md" in out


def test_subagents_get_the_tiered_middleware() -> None:
    import deepagents.graph

    import novacode_cli.agents.core_agent  # noqa: F401 — applies the swap

    assert deepagents.graph.SkillsMiddleware is SubagentSkillsMiddleware
    assert "more skills" in SubagentSkillsMiddleware(
        backend=None, sources=["/s/"]
    )._format_skills_list(SKILLS)


def test_without_the_embedder_specific_keywords_suggest_and_search_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain terms can suggest a candidate without a model download."""
    monkeypatch.setattr(R, "_model", lambda: None)
    R._index_cache.clear()
    mw = _mw()
    state = {"messages": [HumanMessage("review my docker-compose files for security")]}
    assert "docker-reviewer" in str(mw._finish(state, {"skills_metadata": SKILLS})["messages"])
    assert "messages" not in (mw._finish({"messages": [HumanMessage("continue")]}) or {})
    assert "/skills/docker-reviewer/SKILL.md" in mw.tools[0].invoke({"query": "docker compose"})


def test_one_word_messages_get_no_suggestion() -> None:
    state = {"messages": [HumanMessage("clickhouse")]}
    assert "messages" not in (_mw()._finish(state, {"skills_metadata": SKILLS}) or {})
