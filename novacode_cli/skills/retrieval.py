"""Find the skills that fit a request, so they need not all sit in the prompt.

Hybrid search over each skill's name + description: BM25 (exact terms like
"clickhouse", "pptx") fused with static Model2Vec embeddings (paraphrases like
"deck" -> pptx) by reciprocal rank. Skill bodies and supporting references are
loaded separately, only when needed. The embedder is the one ``code_search`` already
downloads; until it has loaded (or if it cannot), search is BM25-only.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from novacode_cli.computation_cache import digest
from novacode_cli.embeddings import MODEL_NAME as MODEL_NAME
from novacode_cli.embeddings import SharedModel, get_model
from novacode_cli.embeddings import local_snapshot as local_snapshot
from novacode_cli.embeddings import prewarm as prewarm

logger = logging.getLogger("nova.skills.retrieval")
SkillT = TypeVar("SkillT", bound=Mapping[str, Any])

# Candidate suggestions, never activation. Absolute similarity and separation
# from ordinary matches work even for a one-skill specialist library.
MIN_COSINE = 0.55
MIN_DENSE_MARGIN = 0.08
MIN_TASK_WORDS = 3
MIN_LEXICAL_TERMS = 2
_GENERIC_TERMS = frozenset(
    "a an and are as at be build can code create do for from help how i improve in "  # noqa: SIM905
    "is it make me my need of on or our please task that the their this to use using "
    "want we with work write you your file files result results output add fix "
    "implement test tests testing review".split()
)

_index_cache: OrderedDict[str, SkillIndex[Any]] = OrderedDict()
_index_lock = threading.Lock()
_MAX_INDEXES = 4  # skills + tools, main agent + subagents


def local_model() -> SharedModel | None:
    """Share installed weights only; call from a worker thread."""
    return get_model()


def _model() -> SharedModel | None:
    """Start skill loading in the background, retaining lexical fallback."""
    return get_model(allow_download=True, wait=False)


def _doc(item: Mapping[str, Any]) -> str:
    name = item["name"].replace("-", " ").replace("_", " ")
    return f"{name}. {item.get('description', '')} {item.get('parameters', '')}".rstrip()


def _tokenize(texts: list[str]) -> Any:  # noqa: ANN401 - bm25s tokenizer types vary.
    import bm25s

    return bm25s.tokenize(
        [text.replace("_", " ") for text in texts],
        stopwords="en",
        show_progress=False,
    )


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[\w]+", text.casefold())) - _GENERIC_TERMS


def _named(query: str, name: str) -> bool:
    return bool(re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", query, re.IGNORECASE))


class SkillIndex(Generic[SkillT]):
    """BM25 + (when available) dense index over skills (or any name + description dicts)."""

    def __init__(self, skills: list[SkillT]) -> None:
        """Index a private metadata snapshot for this exact source inventory."""
        import bm25s

        self.skills = copy.deepcopy(skills)
        docs = [_doc(s) for s in skills]
        self._bm25 = bm25s.BM25()
        self._bm25.index(_tokenize(docs), show_progress=False)
        self._model = _model()
        self._query_cache: tuple[str, Any] | None = None
        self._query_lock = threading.Lock()
        self._emb = None
        if self._model is not None:
            import numpy as np

            emb = self._model.encode(docs)
            self._emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)

    @property
    def dense(self) -> bool:
        """Whether this snapshot has semantic vectors as well as lexical terms."""
        return self._emb is not None

    def _query_vector(self, query: str) -> Any:  # noqa: ANN401 - optional numpy vector.
        """Reuse one normalized query vector without retaining the query text."""
        import numpy as np

        assert self._model is not None  # noqa: S101 - only called by dense index paths
        key = digest(query)
        with self._query_lock:
            cached = self._query_cache
            if cached is not None and cached[0] == key:
                return cached[1]
            vector = self._model.encode([query])[0]
            vector = vector / (np.linalg.norm(vector) + 1e-9)
            self._query_cache = (key, vector)
            return vector

    def search(self, query: str, k: int = 5) -> list[tuple[SkillT, float, float]]:
        """Top ``k`` skills as ``(skill, cosine, bm25)``, best first."""
        import numpy as np

        if not self.skills or not query.strip():
            return []
        n = len(self.skills)
        candidates = min(max(20, k), n)
        idx, scores = self._bm25.retrieve(_tokenize([query]), k=candidates, show_progress=False)
        bm25 = dict(zip(idx[0].tolist(), scores[0].tolist(), strict=True))
        ranked = [i for i, s in bm25.items() if s > 0]
        cosine: dict[int, float] = {}
        if self._emb is not None and self._model is not None:
            sims = self._emb @ self._query_vector(query)
            dense = np.argsort(-sims)[:candidates].tolist()
            cosine = {i: float(sims[i]) for i in dense}
            fused: dict[int, float] = {}
            for rank_list in (ranked, dense):
                for r, i in enumerate(rank_list):
                    fused[i] = fused.get(i, 0.0) + 1 / (60 + r)
            ranked = sorted(fused, key=lambda i: fused[i], reverse=True)
        # An explicit name must not lose to a semantically similar description.
        ranked.sort(key=lambda i: not _named(query, self.skills[i]["name"]))
        return [(self.skills[i], cosine.get(i, 0.0), bm25.get(i, 0.0)) for i in ranked[:k]]

    def suggest(
        self,
        query: str,
        k: int = 3,
        exclude: set[str] | frozenset[str] = frozenset(),
        usage: dict[str, int] | None = None,
    ) -> list[SkillT]:
        """Offer relevant candidates; lexical matches also work without embeddings.

        Usage only breaks ties between equally relevant candidates. A suggestion
        is context for the model to assess, not permission to load or execute.
        """
        import numpy as np

        if not self.skills or not query.strip():
            return []
        baseline = 0.0
        if self._emb is not None and self._model is not None and len(self.skills) > 1:
            baseline = float(np.median(self._emb @ self._query_vector(query)))
        terms = _terms(query)
        candidates = []
        for skill, cosine, _bm25 in self.search(query, k=len(self.skills)):
            name = skill["name"]
            if name in exclude:
                continue
            denied = re.search(
                r"\b(?:do not|don't|never|without|skip|avoid)\s+"
                r"(?:(?:use|using|load|loading)\s+)?(?:the\s+)?[/$]?"
                + re.escape(name)
                + r"(?![\w.-])",
                query,
                re.IGNORECASE,
            )
            if denied:
                continue
            exact = _named(query, name)
            if len(query.split()) < MIN_TASK_WORDS and not exact:
                continue
            overlap = terms & _terms(_doc(skill))
            lexical = len(overlap) >= MIN_LEXICAL_TERMS or bool(terms & _terms(name))
            semantic = (
                len(query.split()) >= MIN_TASK_WORDS
                and cosine >= MIN_COSINE
                and cosine - baseline >= MIN_DENSE_MARGIN
            )
            if exact or lexical or semantic:
                relevance = (exact, len(overlap), round(cosine, 1))
                candidates.append((relevance, (usage or {}).get(name, 0), skill))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [skill for _, _, skill in candidates[:k]]


def get_index(skills: list[SkillT]) -> SkillIndex[SkillT]:
    """A cached index for this exact skill list (rebuilt once the embedder is up)."""
    # The cached index holds source objects, not just vectors. Include their
    # complete identity so a different project or tool metadata cannot leak in.
    key = digest(json.dumps(skills, sort_keys=True, default=str))
    with _index_lock:
        index = _index_cache.get(key)
        if index is not None:
            _index_cache.move_to_end(key)
    model = _model()
    if index is not None and index._model is model:
        return index
    index = SkillIndex(skills)
    with _index_lock:
        _index_cache[key] = index
        _index_cache.move_to_end(key)
        while len(_index_cache) > _MAX_INDEXES:
            _index_cache.popitem(last=False)
    return index


def first_sentence(text: str, limit: int = 150) -> str:
    """Leading sentence of a description, capped at ``limit`` chars."""
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    text = m.group(1) if m else text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def skill_summary(text: str, limit: int = 600) -> str:
    """Preserve scope and exclusions instead of discarding later sentences."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    boundaries = [
        sentence
        for sentence in sentences[1:]
        if re.search(
            r"\b(?:use when|only|not for|do not|don't|skip|never|avoid|trigger)\b",
            sentence,
            re.IGNORECASE,
        )
    ]
    if not boundaries:
        return text[: limit - 1].rstrip() + "…"
    lead = first_sentence(sentences[0], min(150, limit // 3))
    rest = " ".join(boundaries)
    remaining = limit - len(lead) - 1
    return lead + " " + (rest if len(rest) <= remaining else rest[: remaining - 1].rstrip() + "…")
