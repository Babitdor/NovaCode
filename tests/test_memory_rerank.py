"""Semantic candidate retrieval and provider-agnostic memory reranking."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from novacode_cli.memory.search import MemorySearchService

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest


def _service(tmp_path: Path) -> MemorySearchService:
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "websocket-auth.md").write_text(
        "Send the credential in the WebSocket handshake query because browsers cannot set headers.",
        encoding="utf-8",
    )
    (memories / "image-sizing.md").write_text(
        "Use multi-stage builds to keep the Docker image small.",
        encoding="utf-8",
    )
    return MemorySearchService(tmp_path)


def test_semantic_retrieval_finds_a_paraphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")

    def similarities(query: str, candidates: Sequence[str]) -> list[float]:
        assert query == "authenticate a socket upgrade"
        return [0.8 if "handshake" in text else 0.1 for text in candidates]

    monkeypatch.setattr("novacode_cli.memory.semantic.similarities", similarities)
    service = _service(tmp_path)

    result = asyncio.run(service.search("authenticate a socket upgrade", model=None))

    assert "websocket-auth" in result
    assert "image-sizing" not in result


def test_active_model_reranks_only_retrieved_candidate_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    monkeypatch.setattr(
        "novacode_cli.memory.semantic.similarities",
        lambda _query, candidates: [0.9 if "websocket" in text else 0.7 for text in candidates],
    )
    service = _service(tmp_path)
    model = FakeListChatModel(responses=['{"order":["m1","m0"]}'])

    result = asyncio.run(service.search("websocket authentication", model=model))

    assert result.index("image-sizing") < result.index("websocket-auth")
    assert "/memories/memories/image-sizing.md" in result
    assert "/memories/memories/websocket-auth.md" in result


def test_invalid_llm_ranking_uses_local_order_and_reports_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    monkeypatch.setattr(
        "novacode_cli.memory.semantic.similarities",
        lambda _query, candidates: [0.9 if "websocket" in text else 0.7 for text in candidates],
    )
    service = _service(tmp_path)
    model = FakeListChatModel(responses=['{"order":["invented-topic"]}'])

    result = asyncio.run(service.search("websocket authentication", model=model))

    assert result.index("websocket-auth") < result.index("image-sizing")
    assert "invalid ranking" in result


def test_reranking_is_skipped_when_no_active_model_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    monkeypatch.setattr(
        "novacode_cli.memory.semantic.similarities",
        lambda _query, candidates: [0.9 if "websocket" in text else 0.7 for text in candidates],
    )
    service = _service(tmp_path)

    result = asyncio.run(service.search("websocket authentication"))

    assert result.index("websocket-auth") < result.index("image-sizing")
    assert "No active model was available" in result
