"""The embedding cache may be emptied mid-call; a cached text must still come back."""

import numpy as np

from novacode_cli.memory import semantic


class _Model:
    def encode(self, texts):
        return np.ones((len(texts), 3))


def test_a_full_cache_does_not_lose_texts_it_already_held(monkeypatch):
    monkeypatch.setattr(semantic, "_model", _Model())
    monkeypatch.setattr(semantic, "_unavailable", False)
    monkeypatch.setattr(semantic, "_cache", {})
    monkeypatch.setattr(semantic, "_CACHE_LIMIT", 2)

    semantic.embed(["the query", "a"])
    vectors = semantic.embed(["the query", "b", "c"])  # overflows: cache is cleared

    assert vectors.shape == (3, 3)
