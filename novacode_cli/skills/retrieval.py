"""Find the skills that fit a request, so they need not all sit in the prompt.

Hybrid search over each skill's name + description: BM25 (exact terms like
"clickhouse", "pptx") fused with static Model2Vec embeddings (paraphrases like
"deck" -> pptx) by reciprocal rank. Skill bodies and supporting references are
loaded separately, only when needed. The embedder is the one ``code_search`` already
downloads; until it has loaded (or if it cannot), search is BM25-only.
"""

from __future__ import annotations

import concurrent.futures
import copy
import functools
import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Generic, TypeVar

logger = logging.getLogger("nova.skills.retrieval")
SkillT = TypeVar("SkillT", bound=Mapping[str, Any])

MODEL_NAME = "minishlab/potion-code-16M"  # shared with code_search: already cached

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

_executor: concurrent.futures.ThreadPoolExecutor | None = None
_model_future: concurrent.futures.Future | None = None
_index_cache: dict[str, SkillIndex[Any]] = {}


def _cached_model_path() -> str:
    """Return the locally cached snapshot dir for :data:`MODEL_NAME`, or the id.

    ``model2vec`` loads by repo id with ``force_download=True``, which calls
    ``huggingface_hub.snapshot_download`` and makes a metadata round-trip to the
    Hub on *every* launch even when the model is already on disk — that is the
    "Fetching 7 files … 0.00B" bar users see at startup. Asking the Hub for the
    cache only (``local_files_only``) skips the network entirely and loads in
    ~0.16 s instead of ~0.8 s.

    Falls back to the repo id when nothing is cached, so the first run (or a
    cleared cache) still downloads normally.
    """
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        return snapshot_download(MODEL_NAME, local_files_only=True)
    except LocalEntryNotFoundError:
        return MODEL_NAME


def _load_model() -> Any:
    from model2vec import StaticModel

    return StaticModel.from_pretrained(_cached_model_path())


@functools.cache
def local_snapshot() -> str | None:
    """Return installed embedding snapshot identity without a network request."""
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(MODEL_NAME, local_files_only=True)
    except Exception:  # noqa: BLE001 - missing optional local embeddings.
        return None


@functools.cache
def local_model() -> Any | None:  # noqa: ANN401 - optional third-party model.
    """Load existing Model2Vec weights only; call from a worker thread.

    Unlike skill discovery's first-run loader, a knowledge lookup must never
    download weights. A local directory also prevents Model2Vec's forced fetch.
    """
    try:
        from model2vec import StaticModel

        path = local_snapshot()
        return StaticModel.from_pretrained(path) if path is not None else None
    except Exception:  # noqa: BLE001 - exact source-key reuse remains available.
        return None


def _model() -> Any | None:
    """The embedder if it has loaded, else None. Starts loading on first call."""
    global _executor, _model_future
    if _model_future is None:
        _executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="skill-embed")
        _model_future = _executor.submit(_load_model)
    if not _model_future.done():
        return None
    try:
        return _model_future.result()
    except Exception:  # noqa: BLE001 — offline / not installed: BM25 still works
        logger.debug("skill embedder unavailable; BM25 only", exc_info=True)
        return None


def prewarm() -> None:
    """Start loading the embedder in the background (idempotent)."""
    _model()


def _doc(item: Mapping[str, Any]) -> str:
    name = item["name"].replace("-", " ").replace("_", " ")
    return f"{name}. {item.get('description', '')} {item.get('parameters', '')}".rstrip()


def _tokenize(texts: list[str]) -> Any:
    import bm25s

    return bm25s.tokenize(
        [text.replace("_", " ") for text in texts], stopwords="en", show_progress=False,
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
        self._emb = None
        if self._model is not None:
            import numpy as np

            emb = self._model.encode(docs)
            self._emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)

    @property
    def dense(self) -> bool:
        return self._emb is not None

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
            q = self._model.encode([query])[0]
            sims = self._emb @ (q / (np.linalg.norm(q) + 1e-9))
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
        self, query: str, k: int = 3, exclude: set[str] | frozenset[str] = frozenset(),
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
            q = self._model.encode([query])[0]
            baseline = float(np.median(self._emb @ (q / (np.linalg.norm(q) + 1e-9))))
        terms = _terms(query)
        candidates = []
        for skill, cosine, _bm25 in self.search(query, k=len(self.skills)):
            name = skill["name"]
            if name in exclude:
                continue
            denied = re.search(
                r"\b(?:do not|don't|never|without|skip|avoid)\s+"
                r"(?:(?:use|using|load|loading)\s+)?(?:the\s+)?[/$]?"
                + re.escape(name) + r"(?![\w.-])", query, re.IGNORECASE,
            )
            if denied:
                continue
            exact = _named(query, name)
            if len(query.split()) < MIN_TASK_WORDS and not exact:
                continue
            overlap = terms & _terms(_doc(skill))
            lexical = len(overlap) >= MIN_LEXICAL_TERMS or bool(terms & _terms(name))
            semantic = (
                len(query.split()) >= MIN_TASK_WORDS and cosine >= MIN_COSINE
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
    key = json.dumps(skills, sort_keys=True, default=str)
    index = _index_cache.get(key)
    if index is None or (not index.dense and _model() is not None):
        index = SkillIndex(skills)
        if len(_index_cache) >= 4:  # skills + tools, main agent + subagents
            _index_cache.pop(next(iter(_index_cache)))
        _index_cache[key] = index
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
        sentence for sentence in sentences[1:]
        if re.search(
            r"\b(?:use when|only|not for|do not|don't|skip|never|avoid|trigger)\b",
            sentence, re.IGNORECASE,
        )
    ]
    if not boundaries:
        return text[:limit - 1].rstrip() + "…"
    lead = first_sentence(sentences[0], min(150, limit // 3))
    rest = " ".join(boundaries)
    remaining = limit - len(lead) - 1
    return lead + " " + (rest if len(rest) <= remaining else rest[:remaining - 1].rstrip() + "…")
