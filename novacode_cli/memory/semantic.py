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
from contextlib import suppress
from typing import Any

from novacode_cli.computation_cache import digest
from novacode_cli.embeddings import get_model
from novacode_cli.memory.parsed_cache import ParsedMemoryCache

logger = logging.getLogger(__name__)

#: Two bullets at or above this say the same thing.
SAME_FACT = 0.66
#: A lesson at or above this bears on the text it is compared with.
RELEVANT = 0.45

_CACHE_LIMIT = 4096
_cache = ParsedMemoryCache(max_entries=_CACHE_LIMIT, max_bytes=8 * 1024 * 1024)
_cache_model: Any = None
_embed_lock = threading.Lock()


def enabled() -> bool:
    """False when switched off by ``NOVA_SEMANTIC_MEMORY=0`` or the model is missing."""
    if os.environ.get("NOVA_SEMANTIC_MEMORY", "").strip().lower() in {"0", "false", "off"}:
        return False
    return _load() is not None


def _load() -> Any:  # noqa: ANN401
    return get_model()


def embed(texts: list[str]) -> Any:  # noqa: ANN401
    """Unit-length vectors for ``texts`` (rows), or None when unavailable."""
    if not texts or not enabled():
        return None
    import numpy as np

    global _cache_model  # noqa: PLW0603
    model = _load()
    cache_enabled = os.environ.get("NOVA_DISABLE_COMPUTATION_CACHE") != "1"
    # Concurrent memory searches share one miss computation. The lock also
    # protects LRU promotion and eviction; no network calls happen in encode.
    with _embed_lock:
        if _cache_model is not model:
            _cache.clear()
            _cache_model = model
        unique = dict.fromkeys(texts)
        keys = {text: digest(text) for text in unique}
        found = {}
        for text in unique:
            if cache_enabled:
                with suppress(KeyError):
                    found[text] = _cache[keys[text]]
        missing = [text for text in unique if text not in found]
        if missing:
            try:
                vectors = model.encode(missing)
            except Exception:  # noqa: BLE001
                logger.debug("embedding failed", exc_info=True)
                return None
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.where(norms == 0, 1.0, norms)
            for text, vector in zip(missing, vectors, strict=True):
                # A row view would retain the entire batch allocation even if
                # every other row was evicted, defeating the byte budget.
                found[text] = vector.copy()
                if cache_enabled:
                    _cache[keys[text]] = found[text]
        return np.stack([found[text] for text in texts])


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
