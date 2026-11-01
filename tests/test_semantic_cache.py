"""The embedding cache may be emptied mid-call; a cached text must still come back."""

import numpy as np
import pytest

from novacode_cli.memory import semantic
from novacode_cli.memory.parsed_cache import ParsedMemoryCache


class _Model:
    def encode(self, texts):
        return np.ones((len(texts), 3))


@pytest.fixture(autouse=True)
def isolated_embeddings(monkeypatch):
    monkeypatch.setattr(semantic, "_cache", ParsedMemoryCache(max_entries=2, max_bytes=4096))
    monkeypatch.setattr(semantic, "_cache_model", None, raising=False)
    monkeypatch.setenv("NOVA_SEMANTIC_MEMORY", "1")


def test_a_full_cache_does_not_lose_texts_it_already_held(monkeypatch):
    model = _Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)

    semantic.embed(["the query", "a"])
    vectors = semantic.embed(["the query", "b", "c"])  # overflows: cache is cleared

    assert vectors.shape == (3, 3)


def test_recent_embeddings_survive_eviction_without_retaining_input_text(monkeypatch):
    encoded = []

    class Model(_Model):
        def encode(self, texts):
            encoded.extend(texts)
            return super().encode(texts)

    model = Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)
    semantic.embed(["hot", "cold"])
    semantic.embed(["hot"])
    semantic.embed(["new"])
    semantic.embed(["hot"])
    assert encoded == ["hot", "cold", "new"]
    assert len(semantic._cache) == 2
    assert all(len(key) == 64 for key in semantic._cache)
    assert "hot" not in semantic._cache


def test_retained_vectors_do_not_keep_evicted_batch_memory(monkeypatch):
    model = _Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)
    result = semantic.embed([str(n) for n in range(1000)])
    assert result.shape == (1000, 3)
    assert len(semantic._cache) == 2
    assert semantic._cache.resident_bytes <= semantic._cache.max_bytes
    assert all(vector.base is None for vector in semantic._cache.values())
    result[:] = 0
    assert np.linalg.norm(semantic.embed(["999"])[0]) == pytest.approx(1)


def test_model_changes_invalidate_embedding_values(monkeypatch):
    model = _Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)
    before = semantic.embed(["same text"])

    class Model(_Model):
        def encode(self, texts):
            return np.array([[1.0, 0.0, 0.0]] * len(texts))

    model = Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)
    after = semantic.embed(["same text"])
    assert not np.array_equal(before, after)


def test_concurrent_embedding_requests_compute_a_miss_once(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    encoded = []

    class Model(_Model):
        def encode(self, texts):
            encoded.append(list(texts))
            time.sleep(0.01)
            return super().encode(texts)

    model = Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)
    with ThreadPoolExecutor(max_workers=8) as pool:
        vectors = list(pool.map(lambda _: semantic.embed(["shared"]), range(8)))
    assert encoded == [["shared"]]
    assert all(np.array_equal(vectors[0], vector) for vector in vectors)


def test_byte_budget_and_cache_disable_do_not_change_results(monkeypatch):
    encoded = []

    class Model(_Model):
        def encode(self, texts):
            encoded.append(list(texts))
            return np.ones((len(texts), 512))

    model = Model()
    monkeypatch.setattr(semantic, "_load", lambda: model)
    # Each copied vector exceeds this cache's entire byte budget.
    first = semantic.embed(["oversized vector"])
    assert len(semantic._cache) == 0
    assert semantic._cache.resident_bytes == 0
    monkeypatch.setenv("NOVA_DISABLE_COMPUTATION_CACHE", "1")
    second = semantic.embed(["oversized vector"])
    assert np.array_equal(first, second)
    assert encoded == [["oversized vector"], ["oversized vector"]]
