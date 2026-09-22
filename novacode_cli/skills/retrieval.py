"""Find the skills that fit a request, so they need not all sit in the prompt.

Hybrid search over each skill's name + description: BM25 (exact terms like
"clickhouse", "pptx") fused with static Model2Vec embeddings (paraphrases like
"deck" -> pptx) by reciprocal rank. On the real 627-skill library this put the
right skill in the top 3 for 31 of 32 test prompts, at under 1 ms a query; adding
the skill body did not help. The embedder is the one ``code_search`` already
downloads; until it has loaded (or if it cannot), search is BM25-only.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from typing import Any

logger = logging.getLogger("nova.skills.retrieval")

MODEL_NAME = "minishlab/potion-code-16M"  # shared with code_search: already cached

# An unprompted suggestion needs a skill that STANDS OUT: its similarity this
# many standard deviations above the library's mean for the request. Raw scores
# do not work: BM25 grows with query length (every long eval instruction
# "matched" three junk skills) and a lone rare word ("continue") spikes it.
# Measured: real matches z >= 4.8, long task texts <= 3.0, chit-chat mostly < 4.5.
MIN_Z = 4.5
MIN_COSINE = 0.4

_executor: concurrent.futures.ThreadPoolExecutor | None = None
_model_future: concurrent.futures.Future | None = None
_index_cache: dict[tuple, SkillIndex] = {}


def _load_model() -> Any:
    from model2vec import StaticModel

    return StaticModel.from_pretrained(MODEL_NAME)


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


def _doc(item: dict) -> str:
    name = item["name"].replace("-", " ").replace("_", " ")
    return f"{name}. {item.get('description', '')}"


def _tokenize(texts: list[str]) -> Any:
    import bm25s

    return bm25s.tokenize(texts, stopwords="en", show_progress=False)


class SkillIndex:
    """BM25 + (when available) dense index over skills (or any name + description dicts)."""

    def __init__(self, skills: list[dict]) -> None:
        import bm25s

        self.skills = skills
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

    def search(self, query: str, k: int = 5) -> list[tuple[dict, float, float]]:
        """Top ``k`` skills as ``(skill, cosine, bm25)``, best first."""
        import numpy as np

        if not self.skills or not query.strip():
            return []
        n = len(self.skills)
        idx, scores = self._bm25.retrieve(_tokenize([query]), k=min(20, n), show_progress=False)
        bm25 = dict(zip(idx[0].tolist(), scores[0].tolist(), strict=True))
        ranked = [i for i, s in bm25.items() if s > 0]
        cosine: dict[int, float] = {}
        if self._emb is not None:
            q = self._model.encode([query])[0]
            sims = self._emb @ (q / (np.linalg.norm(q) + 1e-9))
            dense = np.argsort(-sims)[:20].tolist()
            cosine = {i: float(sims[i]) for i in dense}
            fused: dict[int, float] = {}
            for rank_list in (ranked, dense):
                for r, i in enumerate(rank_list):
                    fused[i] = fused.get(i, 0.0) + 1 / (60 + r)
            ranked = sorted(fused, key=fused.get, reverse=True)
        return [(self.skills[i], cosine.get(i, 0.0), bm25.get(i, 0.0)) for i in ranked[:k]]

    def suggest(self, query: str, k: int = 3, exclude: set[str] = frozenset()) -> list[dict]:
        """Skills that stand out for ``query``; none without the embedder."""
        import numpy as np

        if self._emb is None or len(query.split()) < 3:
            return []  # BM25 alone cannot tell a match from noise; "continue" is not a task
        q = self._model.encode([query])[0]
        sims = self._emb @ (q / (np.linalg.norm(q) + 1e-9))
        z = (sims - sims.mean()) / (sims.std() + 1e-9)
        out = [
            self.skills[i]
            for i in np.argsort(-sims)[: k + len(exclude)].tolist()
            if z[i] >= MIN_Z and sims[i] >= MIN_COSINE and self.skills[i]["name"] not in exclude
        ]
        return out[:k]


def get_index(skills: list[dict]) -> SkillIndex:
    """A cached index for this exact skill list (rebuilt once the embedder is up)."""
    key = tuple((s["name"], s.get("description", "")) for s in skills)
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
