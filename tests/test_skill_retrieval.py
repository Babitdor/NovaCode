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
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: set())
    R._index_cache.clear()


def _mw(listing_chars: int = 600) -> RefreshingSkillsMiddleware:
    return RefreshingSkillsMiddleware(
        backend=None, sources=["/skills/"], listing_chars=listing_chars
    )


def test_listing_stays_within_budget_and_points_at_search() -> None:
    text = _mw(600)._format_skills_list(SKILLS)
    assert len(text) < 700
    assert "more skills: `skills_search` finds them" in text


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


def test_a_skill_is_not_suggested_twice_in_a_thread() -> None:
    mw, q = _mw(), "optimize my clickhouse database query analytics"
    first = mw._finish({"messages": [HumanMessage(q)]}, {"skills_metadata": SKILLS})["messages"][0]
    again = mw._finish(
        {"messages": [HumanMessage(q), first, AIMessage("done"), HumanMessage(q)]},
        {"skills_metadata": SKILLS},
    )
    assert "clickhouse-io" not in str((again or {}).get("messages", ""))


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


def test_without_the_embedder_nothing_is_suggested_but_search_works(monkeypatch) -> None:
    """BM25 alone cannot tell a match from noise ("continue" spikes it)."""
    monkeypatch.setattr(R, "_model", lambda: None)
    R._index_cache.clear()
    mw = _mw()
    state = {"messages": [HumanMessage("review my docker-compose files for security")]}
    assert "messages" not in (mw._finish(state, {"skills_metadata": SKILLS}) or {})
    assert "/skills/docker-reviewer/SKILL.md" in mw.tools[0].invoke({"query": "docker compose"})


def test_one_word_messages_get_no_suggestion() -> None:
    state = {"messages": [HumanMessage("clickhouse")]}
    assert "messages" not in (_mw()._finish(state, {"skills_metadata": SKILLS}) or {})


# ---------------------------------------------------------------------------
# Cache-first model load — no Hub round-trip at startup
# ---------------------------------------------------------------------------


def test_cached_model_path_prefers_local_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached model resolves to its local dir, so loading never hits the Hub.

    Loading by repo id (``from_pretrained(MODEL_NAME)``) makes
    ``snapshot_download`` re-validate every file over the network on each
    launch, which is what prints the startup "Fetching 7 files … 0.00B" bar.
    """
    snapshot = "/cache/models--minishlab--potion-code-16M/snapshots/abc"

    def fake_snapshot(*_args: object, **_kwargs: object) -> str:
        return snapshot

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot)

    assert R._cached_model_path() == snapshot


def test_cached_model_path_requests_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup must be cache-only, or it is the very round-trip we removed."""
    seen: dict = {}

    def fake_snapshot(model: str, **kwargs: object) -> str:
        seen["model"] = model
        seen["kwargs"] = kwargs
        return "/cache/snapshot"

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot)
    R._cached_model_path()

    assert seen["model"] == R.MODEL_NAME
    assert seen["kwargs"].get("local_files_only") is True


def test_cached_model_path_falls_back_to_repo_id_when_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold cache (first run, or cleared) must still download normally."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    def missing(*_args: object, **_kwargs: object) -> None:
        msg = "not cached"
        raise LocalEntryNotFoundError(msg)

    monkeypatch.setattr("huggingface_hub.snapshot_download", missing)

    assert R._cached_model_path() == R.MODEL_NAME


def test_load_model_uses_the_cached_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_load_model`` loads whatever ``_cached_model_path`` resolved."""
    loaded: dict = {}

    class _Model:
        @staticmethod
        def from_pretrained(path: str) -> str:
            loaded["path"] = path
            return "MODEL"

    import model2vec

    monkeypatch.setattr(R, "_cached_model_path", lambda: "/resolved/snapshot")
    monkeypatch.setattr(model2vec, "StaticModel", _Model)

    assert R._load_model() == "MODEL"
    assert loaded["path"] == "/resolved/snapshot"
