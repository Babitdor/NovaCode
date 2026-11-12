"""Scoped, semantic candidate retrieval for explicit memory searches."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.memory.parsed_cache import MemoryCorpus, ParsedMemoryCache, file_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from langchain_core.language_models import BaseChatModel

    from novacode_cli.hermes.ledger import LearningRecord
    from novacode_cli.memory.rerank import MemoryReranker

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "after",
        "again",
        "against",
        "already",
        "also",
        "another",
        "because",
        "been",
        "being",
        "could",
        "does",
        "doing",
        "done",
        "each",
        "else",
        "even",
        "ever",
        "from",
        "have",
        "here",
        "into",
        "just",
        "like",
        "made",
        "make",
        "many",
        "more",
        "most",
        "much",
        "need",
        "only",
        "other",
        "over",
        "please",
        "question",
        "really",
        "should",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "this",
        "those",
        "unrelated",
        "very",
        "want",
        "well",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "would",
        "your",
        "yours",
    }
)
_MIN_LEXICAL_RELEVANCE = 2
_MIN_STRONG_LEXICAL_RELEVANCE = 3
_MIN_SEMANTIC_RELEVANCE = 0.45
_MIN_CANDIDATES_TO_RERANK = 2
_MIN_TOKEN_CHARS = 4
_MAX_RESULTS = 20
_EXCERPT_CHARS = 240
_MAX_RERANK_CANDIDATES = 20


@dataclass(frozen=True)
class MemoryCandidate:
    """A trusted memory result with an excerpt selected from its source."""

    topic: str
    excerpt: str
    source: str | None
    relevance: float


def _tokens(text: str) -> frozenset[str]:
    """Lowercased alphanumeric words of length >= 4, minus generic stopwords."""
    return frozenset(
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(word) >= _MIN_TOKEN_CHARS and word not in _STOPWORDS
    )


def search_excerpt(body: str, query_tokens: frozenset[str]) -> str:
    """Return a short passage from the body that best matches an explicit search."""
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if query_tokens:
        ranked = sorted(
            ((len(query_tokens & _tokens(line)), index, line) for index, line in enumerate(lines)),
            key=lambda item: (-item[0], item[1]),
        )
        matches = [line for score, _, line in ranked if score]
        excerpt = " ".join(matches) if matches else " ".join(body.strip().split())
    else:
        excerpt = " ".join(body.strip().split())
    if len(excerpt) > _EXCERPT_CHARS:
        return excerpt[:_EXCERPT_CHARS].rstrip() + "…"
    return excerpt


class MemorySearchService:
    """Load scoped memory sources, retrieve candidates, and optionally rerank them."""

    def __init__(
        self,
        agent_dir: Path,
        *,
        project_key: str = "",
        reranker: MemoryReranker | None = None,
    ) -> None:
        """Bind corpus loading to one agent and its current project scope."""
        self.agent_dir = Path(agent_dir)
        self.project_key = project_key
        if reranker is None:
            from novacode_cli.memory.rerank import MemoryReranker

            reranker = MemoryReranker()
        self._reranker = reranker
        self._corpus_cache: Mapping[str, tuple[frozenset[str], str, frozenset[str]]] | None = None
        self._corpus_sig: tuple | None = None
        self._parsed_memory_cache: ParsedMemoryCache | None = None

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        model: BaseChatModel | None = None,
    ) -> str:
        """Search locally, then rerank a bounded shortlist with the active model."""
        result_limit = max(1, min(limit, _MAX_RESULTS))
        shortlist_limit = min(result_limit * 2, _MAX_RERANK_CANDIDATES)
        corpus = await asyncio.to_thread(self._load_memory_corpus)
        if not corpus:
            return "No topic memory yet. Proceed without it."
        candidates = await asyncio.to_thread(self._retrieve, query, shortlist_limit, corpus)
        if not candidates:
            return f"No memory matches {query!r}. Proceed without it."

        fallback_note = ""
        if model is not None and len(candidates) >= _MIN_CANDIDATES_TO_RERANK:
            from novacode_cli.memory.rerank import InvalidMemoryRankingError

            try:
                candidates = await self._reranker.rank(query, candidates, model=model)
            except InvalidMemoryRankingError as exc:
                logger.warning("Memory reranker returned an invalid ranking: %s", exc)
                fallback_note = (
                    "\n\nLLM reranking returned an invalid ranking; showing local relevance order."
                )
            except Exception as exc:  # noqa: BLE001 - optional provider reranking must degrade safely
                logger.warning("Memory reranking failed; showing local relevance order: %s", exc)
                fallback_note = "\n\nLLM reranking failed; showing local relevance order."
        elif model is None and len(candidates) >= _MIN_CANDIDATES_TO_RERANK:
            fallback_note = (
                "\n\nNo active model was available for reranking; showing local relevance order."
            )

        return self._format(candidates[:result_limit]) + fallback_note

    def _retrieve(
        self,
        query: str,
        limit: int,
        corpus: Mapping[str, tuple[frozenset[str], str, frozenset[str]]],
    ) -> list[MemoryCandidate]:
        """Rank the corpus locally, using embeddings when they are available."""
        query_tokens = _tokens(query)
        exact_topic = query.strip().lower().replace(" ", "-").replace("_", "-")
        rows: list[tuple[str, str, int]] = []
        descriptors: list[str] = []
        for topic, (title_tokens, body, body_tokens) in corpus.items():
            lexical = 2 * len(query_tokens & title_tokens) + len(query_tokens & body_tokens)
            excerpt = search_excerpt(body, query_tokens)
            rows.append((topic, excerpt, lexical))
            descriptors.append(f"{topic.replace('-', ' ')}\n{body[:1200]}")

        semantic_scores: list[float] = []
        if query.strip():
            from novacode_cli.memory import semantic

            semantic_scores = semantic.similarities(query, descriptors)

        scored: list[tuple[float, str, str]] = []
        has_semantics = len(semantic_scores) == len(rows)
        for index, (topic, excerpt, lexical) in enumerate(rows):
            is_exact = topic == exact_topic
            semantic_score = semantic_scores[index] if has_semantics else 0.0
            if not math.isfinite(semantic_score):
                semantic_score = 0.0
            if is_exact:
                score = 1.0 + lexical
            elif has_semantics:
                # One title token counts twice; it should not override negative
                # semantic evidence by itself. Preserve strong lexical matches.
                if (
                    semantic_score < _MIN_SEMANTIC_RELEVANCE
                    and lexical < _MIN_STRONG_LEXICAL_RELEVANCE
                ):
                    continue
                score = semantic_score + lexical * 0.01
            else:
                if lexical < _MIN_LEXICAL_RELEVANCE:
                    continue
                score = lexical * 0.1
            scored.append((score, topic, excerpt))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            MemoryCandidate(
                topic=topic,
                excerpt=excerpt,
                source=self._topic_path(topic),
                relevance=score,
            )
            for score, topic, excerpt in scored[:limit]
        ]

    def _load_memory_corpus(
        self,
    ) -> Mapping[str, tuple[frozenset[str], str, frozenset[str]]]:
        """Load and cache the general, current-project, and activated memories."""
        mem_dir = self.agent_dir / "memories"
        dirs = [mem_dir]
        if self.project_key:
            dirs.append(mem_dir / "projects" / self.project_key)
        records, imported = self._load_learning_records()
        captured = self._active_artifacts()
        if captured is not None:
            records = []
        signature = self._corpus_signature_for(
            dirs,
            records,
            imported,
            captured,
        )
        if self._corpus_cache is not None and self._corpus_sig == signature:
            return self._corpus_cache

        corpus = self._new_corpus()
        self._add_file_memories(corpus, dirs, mem_dir, imported)
        self._add_generated_memories(corpus, records, captured)
        self._add_builtin_lessons(corpus)
        self._corpus_cache, self._corpus_sig = corpus, signature
        return corpus

    def _load_learning_records(self) -> tuple[list[LearningRecord], set[str]]:
        """Load activated ledger entries and legacy file references."""
        from novacode_cli.hermes.ledger import LearningLedger

        ledger_path = self.agent_dir / "learning.sqlite3"
        if not ledger_path.exists():
            return [], set()
        ledger = LearningLedger(ledger_path)
        records = ledger.list(project=self.project_key, active=True)
        imported = {
            record.evidence[0]
            for record in ledger.list()
            if record.origin == "legacy" and record.evidence
        }
        return records, imported

    @staticmethod
    def _active_artifacts() -> tuple[tuple[str, str, str], ...] | None:
        """Read the activation snapshot bound to this run, when one exists."""
        from novacode_cli.hermes.activation import active_artifacts
        from novacode_cli.session.runtime_identity import current_identity

        return active_artifacts.get() if current_identity.get() else None

    @staticmethod
    def _corpus_signature_for(
        dirs: Sequence[Path],
        records: Sequence[LearningRecord],
        imported: set[str],
        captured: tuple[tuple[str, str, str], ...] | None,
    ) -> tuple:
        """Include every source of corpus identity in the cache key."""
        from novacode_cli.hermes.activation import content_hash

        return (
            *(MemorySearchService.corpus_signature(directory) for directory in dirs),
            tuple(record.reference for record in records),
            tuple(sorted(imported)),
            tuple(
                (kind, target, content_hash(content.encode())) for kind, target, content in captured
            )
            if captured is not None
            else None,
        )

    def _new_corpus(self) -> MemoryCorpus:
        """Create a corpus backed by the bounded parsed-memory cache."""
        if self._parsed_memory_cache is None:
            self._parsed_memory_cache = ParsedMemoryCache()
        return MemoryCorpus(self._parsed_memory_cache, _tokens)

    @staticmethod
    def _add_file_memories(
        corpus: MemoryCorpus,
        dirs: Sequence[Path],
        mem_dir: Path,
        imported: set[str],
    ) -> None:
        """Register file-backed pages while excluding generated navigation maps."""
        from novacode_cli.memory.wiki import HUBS, MANAGED

        for path in (
            path for directory in dirs if directory.is_dir() for path in directory.glob("*.md")
        ):
            if str(path) in imported or path.name == "INDEX.md":
                continue
            try:
                if path.stem in {f"wiki-{group}" for group in HUBS}:
                    with path.open("rb") as source:
                        if MANAGED.encode() in source.read(256):
                            continue
                digest = file_hash(path)
            except OSError:
                continue
            title_tokens = _tokens(path.stem.replace("-", " ").replace("_", " "))
            topic = path.relative_to(mem_dir).with_suffix("").as_posix()
            corpus.add(topic, title_tokens, path, digest)

    @staticmethod
    def _add_generated_memories(
        corpus: MemoryCorpus,
        records: Sequence[LearningRecord],
        captured: tuple[tuple[str, str, str], ...] | None,
    ) -> None:
        """Add generated records separately from file-backed user memory."""
        from novacode_cli.hermes.activation import content_hash

        for record in records:
            title = record.applicability or record.kind
            corpus.add(
                f"learning-{record.id}",
                _tokens(title),
                record.content,
                content_hash(record.content.encode()),
            )
        for index, (kind, target, content) in enumerate(captured or ()):
            if kind in {"lesson", "habit"}:
                corpus.add(
                    f"activated-{index}",
                    _tokens(target or kind),
                    content,
                    content_hash(content.encode()),
                )

    @staticmethod
    def _add_builtin_lessons(corpus: MemoryCorpus) -> None:
        """Register built-in lessons unless a learned page supersedes them."""
        from novacode_cli.memory import builtin_lessons

        if builtin_lessons.enabled() and builtin_lessons.TOPIC not in corpus:
            text = builtin_lessons.body()
            from novacode_cli.hermes.activation import content_hash

            corpus.add(builtin_lessons.TOPIC, frozenset(), text, content_hash(text.encode()))

    @staticmethod
    def corpus_signature(mem_dir: Path) -> tuple | None:
        """Return names and contents, including renames and restored timestamps."""
        try:
            with os.scandir(mem_dir) as entries:
                names = sorted(entry.name for entry in entries if entry.name.endswith(".md"))
            return tuple((name, file_hash(mem_dir / name)) for name in names)
        except FileNotFoundError:
            return ()
        except OSError:
            return None

    def _topic_path(self, topic: str) -> str | None:
        """Expose a real source path; generated knowledge must not invent pages."""
        source = getattr(self._corpus_cache, "sources", {}).get(topic)
        if source and isinstance(source[1], Path):
            return "/memories/" + source[1].relative_to(self.agent_dir).as_posix()
        return None

    @staticmethod
    def _format(candidates: Sequence[MemoryCandidate]) -> str:
        """Render trusted candidate excerpts with their real or absent source."""
        lines = []
        for candidate in candidates:
            source = (
                f"`{candidate.source}`"
                if candidate.source
                else "(activated knowledge; no backing file)"
            )
            lines.append(f"- **{candidate.topic}** {source}: {candidate.excerpt}")
        return "\n".join(lines)
