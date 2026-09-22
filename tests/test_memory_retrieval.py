"""Per-turn memory retrieval: relevant learned lesson BODIES (not just INDEX
pointers) must be injected for the current request, so accumulated learning
actually reaches the model instead of sitting inert on disk."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from novacode_cli.memory.agent_memory import AgentMemoryMiddleware


def _mw(agent_dir: Path) -> AgentMemoryMiddleware:
    """Build a middleware bound only to agent_dir (retrieval touches nothing else)."""
    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = agent_dir
    mw._corpus_cache = None
    mw._corpus_sig = None
    mw._retrieval_cache = None
    return mw


def _req(text: str) -> SimpleNamespace:
    return SimpleNamespace(messages=[{"role": "user", "content": text}])


def _setup(tmp_path: Path) -> Path:
    mem = tmp_path / "memories"
    mem.mkdir(parents=True)
    (mem / "INDEX.md").write_text("# Memory Index\n- pointers only\n", encoding="utf-8")
    (mem / "websocket-token-auth.md").write_text(
        "- The cowork WebSocket takes the token as a query param because browsers "
        "cannot set headers on a WebSocket handshake.\n",
        encoding="utf-8",
    )
    (mem / "docker-image-sizing.md").write_text(
        "- Multi-stage builds keep the final Docker image under 200MB.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_relevant_lesson_body_is_injected(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    out = mw._relevant_memories(_req("how do I pass a token to authenticate the websocket handshake?"))
    assert "websocket-token-auth" in out
    assert "query param because browsers" in out  # the BODY, not just a pointer
    assert "docker-image-sizing" not in out  # irrelevant topic excluded
    assert "INDEX" not in out  # the index file is never treated as a lesson


def test_unrelated_query_injects_nothing(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    assert mw._relevant_memories(_req("what is the capital of France?")) == ""


def test_retrieval_is_cached_per_query(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    q = "websocket token handshake authenticate"
    first = mw._relevant_memories(_req(q))
    assert first  # something matched
    # Corrupt the corpus cache; a cached query must NOT re-scan (returns same).
    mw._corpus_cache = {}
    assert mw._relevant_memories(_req(q)) == first


def test_empty_when_no_user_message(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    assert mw._relevant_memories(SimpleNamespace(messages=[])) == ""
