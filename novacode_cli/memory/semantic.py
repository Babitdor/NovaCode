"""Meaning-level similarity between short texts, computed locally.

Lesson handling used to compare words: is this bullet a repeat of that one, has
another project recorded the same fact, does this lesson bear on the error on
screen. Word overlap got each of those wrong on real data — it missed four of
five same-fact pairs, "confirmed" a toolchain note because it shared the phrase
``apt-get update``, and could not connect ``python3: command not found`` to a
lesson that only described the fix.

This uses the static embedding model Nova already depends on for code search
(``potion-code-16M`` via ``model2vec``): no network or API call per use, ~1 s to
load once, and a few hundred texts embed in ~20 ms. If it cannot be loaded every
caller falls back to word overlap, so nothing depends on it being present.

Measured on lessons from Terminal-Bench runs (cosine):
same fact reworded 0.59-0.81; different facts on one subject 0.34-0.47 (one pair
0.65); unrelated ~0.0; the error a lesson is about 0.50-0.70; ordinary output
against every lesson under 0.2.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

MODEL_NAME = "minishlab/potion-code-16M"

#: Two bullets at or above this say the same thing.
SAME_FACT = 0.66
#: A lesson at or above this bears on the text it is compared with.
RELEVANT = 0.45

_lock = threading.Lock()
_model: Any = None
_unavailable = False
_cache: dict[str, Any] = {}
_CACHE_LIMIT = 4096


def enabled() -> bool:
    """False when switched off by ``NOVA_SEMANTIC_MEMORY=0`` or the model is missing."""
    if os.environ.get("NOVA_SEMANTIC_MEMORY", "").strip().lower() in {"0", "false", "off"}:
        return False
    return _load() is not None


def _load() -> Any:  # noqa: ANN401
    global _model, _unavailable  # noqa: PLW0603
    if _model is not None or _unavailable:
        return _model
    with _lock:
        if _model is not None or _unavailable:
            return _model
        try:
            from model2vec import StaticModel

            _model = StaticModel.from_pretrained(MODEL_NAME)
        except Exception:  # noqa: BLE001 — offline, not installed, corrupt cache
            logger.debug("semantic memory unavailable; using word overlap", exc_info=True)
            _unavailable = True
    return _model


def embed(texts: list[str]) -> Any:  # noqa: ANN401
    """Unit-length vectors for ``texts`` (rows), or None when unavailable."""
    if not texts or not enabled():
        return None
    import numpy as np

    missing = [t for t in dict.fromkeys(texts) if t not in _cache]
    if missing:
        try:
            vectors = _load().encode(missing)
        except Exception:  # noqa: BLE001
            logger.debug("embedding failed", exc_info=True)
            return None
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1.0, norms)
        if len(_cache) + len(missing) > _CACHE_LIMIT:
            _cache.clear()
        for text, vector in zip(missing, vectors, strict=True):
            _cache[text] = vector
    return np.stack([_cache[t] for t in texts])


def best_match(text: str, candidates: list[str]) -> tuple[float, int]:
    """``(similarity, index)`` of the candidate closest to ``text``; ``(0.0, -1)`` if none."""
    if not text.strip() or not candidates:
        return 0.0, -1
    vectors = embed([text, *candidates])
    if vectors is None:
        return 0.0, -1
    scores = vectors[1:] @ vectors[0]
    index = int(scores.argmax())
    return float(scores[index]), index


def similarities(text: str, candidates: list[str]) -> list[float]:
    """Similarity of ``text`` to each candidate; empty when unavailable."""
    if not text.strip() or not candidates:
        return []
    vectors = embed([text, *candidates])
    if vectors is None:
        return []
    return [float(s) for s in vectors[1:] @ vectors[0]]
