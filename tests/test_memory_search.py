"""Memory progressive disclosure: a small index slice + a `memory_search` tool.

The topic index (``INDEX.md``) can be ~100k chars of pointers. Injecting all of
it billed the model every turn, so only a small newest-first slice is injected
and ``memory_search`` finds the rest (and the topic bodies) on demand.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from novacode_cli.memory.agent_memory import AgentMemoryMiddleware
from novacode_cli.memory.limits import (
    DEFAULT_MEMORY_BLOCK_CHARS,
    DEFAULT_MEMORY_INDEX_CHARS,
    MAX_MEMORY_CHARS,
    memory_budget,
)


def _mw(agent_dir: Path, *, index_chars: int | None = None) -> AgentMemoryMiddleware:
    """Build a middleware bound only to agent_dir (search touches nothing else)."""
    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = agent_dir
    mw._corpus_cache = None
    mw._corpus_sig = None
    mw._index_chars = index_chars or DEFAULT_MEMORY_INDEX_CHARS
    return mw


def _setup(tmp_path: Path) -> Path:
    mem = tmp_path / "memories"
    mem.mkdir(parents=True)
    (mem / "INDEX.md").write_text(
        "# Memory Index\n\n" + "\n".join(f"- [topic-{i}](topic-{i}.md)" for i in range(400)),
        encoding="utf-8",
    )
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


# ── the search tool ─────────────────────────────────────────────────────────


def test_memory_search_returns_the_matching_topic_path(tmp_path: Path) -> None:
    mw = _mw(_setup(tmp_path))
    out = asyncio.run(
        mw._memory_search("how do I authenticate the websocket handshake with a token?")
    )
    assert "websocket-token-auth" in out
    assert "/memories/memories/websocket-token-auth.md" in out
    assert "docker-image-sizing" not in out


def test_memory_search_finds_a_topic_by_exact_name(tmp_path: Path) -> None:
    mw = _mw(_setup(tmp_path))
    out = asyncio.run(mw._memory_search("docker-image-sizing"))
    assert "/memories/memories/docker-image-sizing.md" in out


def test_memory_search_reports_no_match(tmp_path: Path) -> None:
    mw = _mw(_setup(tmp_path))
    assert "No memory matches" in asyncio.run(
        mw._memory_search("what is the capital of France?")
    )


def test_memory_search_with_no_corpus(tmp_path: Path) -> None:
    mw = _mw(tmp_path)
    assert "No topic memory" in asyncio.run(mw._memory_search("anything"))


def test_the_tool_is_registered_as_async(tmp_path: Path) -> None:
    """A sync tool would read the whole corpus on the event loop."""
    from novacode_cli.config.config import settings

    mw = AgentMemoryMiddleware(settings=settings, assistant_id="nova-agent")
    tool = next(t for t in mw.tools if t.name == "memory_search")
    assert tool.coroutine is not None


# ── the index slice ─────────────────────────────────────────────────────────


def test_index_is_capped_to_its_own_smaller_budget(tmp_path: Path) -> None:
    mw = _mw(_setup(tmp_path), index_chars=500)
    big = (tmp_path / "memories" / "INDEX.md").read_text(encoding="utf-8")
    assert len(big) > 500
    capped = mw._cap_index(big)
    assert len(capped) < 700
    assert "memory_search" in capped  # tells the agent how to reach the rest


def test_a_short_index_is_left_intact(tmp_path: Path) -> None:
    mw = _mw(tmp_path, index_chars=5_000)
    small = "# Memory Index\n- one\n"
    assert mw._cap_index(small) == small


# ── the configurable budget ─────────────────────────────────────────────────


def test_memory_budget_honours_a_custom_cap() -> None:
    assert memory_budget(1_048_576, cap=6_000) == 6_000
    assert memory_budget(1_048_576, cap=3_000) == 3_000
    # The window fraction still protects a small window.
    assert memory_budget(40_000, cap=6_000) == 4_800


def test_memory_budget_defaults_to_the_lowered_cap() -> None:
    assert DEFAULT_MEMORY_BLOCK_CHARS == 6_000
    assert memory_budget(0) == 6_000
    # The disk/compaction cap stays higher, so raising the budget recovers content.
    assert MAX_MEMORY_CHARS == 12_000


def test_nova_config_exposes_the_memory_budgets() -> None:
    from novacode_cli.config.nova_config import NovaConfig

    cfg = NovaConfig()
    assert cfg.get_memory_block_chars() == DEFAULT_MEMORY_BLOCK_CHARS
    assert cfg.get_memory_index_chars() == DEFAULT_MEMORY_INDEX_CHARS
    # A configured value wins.
    cfg._config["memory_block_chars"] = 3_500
    cfg._config["memory_index_chars"] = 1_200
    assert cfg.get_memory_block_chars() == 3_500
    assert cfg.get_memory_index_chars() == 1_200
