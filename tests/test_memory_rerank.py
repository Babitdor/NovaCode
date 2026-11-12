"""Semantic candidate retrieval and provider-agnostic memory reranking."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from novacode_cli.memory.search import MemorySearchService

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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


@pytest.mark.parametrize("scores", [[0.1, 0.1], [float("nan"), float("nan")], [], [0.9]])
def test_weak_lexical_overlap_requires_semantic_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scores: list[float]
) -> None:
    monkeypatch.setattr("novacode_cli.memory.semantic.similarities", lambda *_: scores)
    service = MemorySearchService(tmp_path)
    corpus = {
        "docker-sizing": (frozenset({"docker", "sizing"}), "Small images", frozenset()),
        "socket-auth": (frozenset({"socket"}), "Secure handshake", frozenset({"secure"})),
    }

    candidates = service._retrieve("docker credentials", 5, corpus)

    # A single title token counts twice. Embeddings can reject that weak match;
    # missing or incomplete embeddings must retain the lexical fallback.
    assert [candidate.topic for candidate in candidates] == (
        [] if len(scores) == len(corpus) else ["docker-sizing"]
    )


def test_strong_lexical_and_exact_matches_survive_low_semantic_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("novacode_cli.memory.semantic.similarities", lambda *_: [0.0])
    service = MemorySearchService(tmp_path)
    corpus = {
        "docker-sizing": (
            frozenset({"docker", "sizing"}),
            "Docker builds",
            frozenset({"docker", "builds"}),
        ),
    }

    assert service._retrieve("docker builds", 5, corpus)[0].topic == "docker-sizing"
    assert service._retrieve("docker sizing", 5, corpus)[0].topic == "docker-sizing"


def test_lexical_fallback_rejects_single_body_match_and_ranks_stronger_matches_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("novacode_cli.memory.semantic.similarities", lambda *_: [])
    service = MemorySearchService(tmp_path)
    corpus = {
        "noise": (frozenset(), "Docker", frozenset({"docker"})),
        "title-match": (frozenset({"docker"}), "Images", frozenset()),
        "body-match": (frozenset(), "Docker builds", frozenset({"docker", "builds"})),
    }

    candidates = service._retrieve("docker builds", 5, corpus)

    assert [candidate.topic for candidate in candidates] == ["body-match", "title-match"]
    assert [candidate.relevance for candidate in candidates] == [0.2, 0.2]


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


@pytest.mark.parametrize("failure", [ValueError, RuntimeError, TimeoutError])
async def test_provider_reranking_errors_preserve_local_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    monkeypatch.setattr(
        "novacode_cli.memory.semantic.similarities",
        lambda _query, candidates: [0.9 if "websocket" in text else 0.7 for text in candidates],
    )
    service = _service(tmp_path)

    async def fail(*_args: object, **_kwargs: object) -> list:
        message = "Provider rejected the reranking request"
        raise failure(message)

    monkeypatch.setattr(service._reranker, "rank", fail)
    result = await service.search("websocket authentication", model=FakeListChatModel(responses=[]))

    assert result.index("websocket-auth") < result.index("image-sizing")
    assert "LLM reranking failed" in result
