"""Embedding consumers share local weights without hiding failed installations."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from novacode_cli import embeddings


@pytest.fixture(autouse=True)
def isolated_owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(embeddings, "_future", None)
    monkeypatch.setattr(embeddings, "_snapshot", None)
    monkeypatch.setattr(embeddings, "_retry_at", 0)
    executor = ThreadPoolExecutor(1)
    monkeypatch.setattr(embeddings, "_executor", executor)
    yield
    executor.shutdown(wait=True)


def test_concurrent_consumers_load_once_and_serialize_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    loads = []
    encoding = Lock()

    class Model:
        def encode(self, texts: list[str]) -> list[str]:
            assert encoding.acquire(blocking=False)
            try:
                time.sleep(0.005)
                return texts
            finally:
                encoding.release()

    def load(snapshot: str | None) -> embeddings.SharedModel:
        loads.append(snapshot)
        time.sleep(0.02)
        return embeddings.SharedModel(Model(), snapshot)

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: "snapshot-a")
    monkeypatch.setattr(embeddings, "_load", load)
    with ThreadPoolExecutor(8) as workers:
        models = list(workers.map(lambda _: embeddings.get_model(), range(8)))
        assert len({id(model) for model in models}) == 1
        assert list(workers.map(lambda model: model.encode(["query"]), models)) == [["query"]] * 8
    assert loads == ["snapshot-a"]


def test_local_reader_never_downloads_and_recovers_after_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = [None]
    loads = []
    monkeypatch.setattr(embeddings, "local_snapshot", lambda: installed[0])
    monkeypatch.setattr(
        embeddings, "_load", lambda path: loads.append(path) or embeddings.SharedModel(None, path)
    )
    assert embeddings.get_model() is None
    assert loads == []
    installed[0] = "snapshot-a"
    assert embeddings.get_model().snapshot == "snapshot-a"
    installed[0] = "snapshot-b"
    assert embeddings.get_model().snapshot == "snapshot-b"
    assert loads == ["snapshot-a", "snapshot-b"]


def test_failed_load_backs_off_but_new_snapshot_retries_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = ["bad"]
    loads = []

    def load(snapshot: str | None) -> embeddings.SharedModel:
        loads.append(snapshot)
        if snapshot == "bad":
            message = "incomplete weights"
            raise OSError(message)
        return embeddings.SharedModel(None, snapshot)

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: installed[0])
    monkeypatch.setattr(embeddings, "_load", load)
    assert embeddings.get_model() is None
    assert embeddings.get_model() is None
    assert loads == ["bad"]
    installed[0] = "good"
    assert embeddings.get_model().snapshot == "good"
    assert loads == ["bad", "good"]


def test_failed_load_retries_after_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    loads = []

    def load(snapshot: str | None) -> embeddings.SharedModel:
        loads.append(snapshot)
        if len(loads) == 1:
            message = "temporary read failure"
            raise OSError(message)
        return embeddings.SharedModel(None, snapshot)

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: "snapshot")
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(embeddings, "_load", load)
    assert embeddings.get_model() is None
    assert embeddings.get_model() is None
    now[0] += embeddings._FAILURE_BACKOFF
    assert embeddings.get_model().snapshot == "snapshot"


def test_knowledge_falls_back_while_skills_download_and_then_shares_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.memory import semantic
    from novacode_cli.skills import retrieval

    installed = [None]
    loads = []
    started, finish = Event(), Event()

    def load(_snapshot: str | None) -> embeddings.SharedModel:
        loads.append(_snapshot)
        started.set()
        assert finish.wait(2)
        installed[0] = "downloaded"
        return embeddings.SharedModel(None, installed[0])

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: installed[0])
    monkeypatch.setattr(embeddings, "_load", load)
    try:
        assert retrieval._model() is None
        assert started.wait(2)
        assert retrieval.local_model() is None
        assert semantic._load() is None
    finally:
        finish.set()
    embeddings._future.result(timeout=2)
    assert retrieval.local_model() is semantic._load() is retrieval._model()
    assert loads == [None]


def test_snapshot_changes_during_load_never_return_old_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = ["snapshot-a"]
    started, finish = Event(), Event()
    loads = []

    def load(snapshot: str | None) -> embeddings.SharedModel:
        loads.append(snapshot)
        if snapshot == "snapshot-a":
            started.set()
            assert finish.wait(2)
        return embeddings.SharedModel(None, snapshot)

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: installed[0])
    monkeypatch.setattr(embeddings, "_load", load)
    with ThreadPoolExecutor(1) as consumers:
        first = consumers.submit(embeddings.get_model)
        try:
            assert started.wait(2)
            installed[0] = "snapshot-b"
            assert embeddings.get_model() is None
        finally:
            finish.set()
        assert first.result(timeout=2) is None
    assert embeddings.get_model().snapshot == "snapshot-b"
    assert loads == ["snapshot-a", "snapshot-b"]


def test_local_reader_does_not_join_first_download_when_snapshot_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = [None]
    started, finish = Event(), Event()

    def load(_snapshot: str | None) -> embeddings.SharedModel:
        installed[0] = "downloaded"
        started.set()
        assert finish.wait(2)
        return embeddings.SharedModel(None, installed[0])

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: installed[0])
    monkeypatch.setattr(embeddings, "_load", load)
    try:
        assert embeddings.get_model(allow_download=True, wait=False) is None
        assert started.wait(2)
        assert embeddings.get_model() is None
    finally:
        finish.set()


def test_download_loads_and_labels_the_same_immutable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    model = object()
    download = Mock(return_value="/cache/snapshot-a")
    load = Mock(return_value=model)
    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    monkeypatch.setattr("model2vec.StaticModel.from_pretrained", load)
    monkeypatch.setattr(embeddings, "local_snapshot", lambda: "/cache/snapshot-b")
    result = embeddings._load(None)
    assert result.model is model
    assert result.snapshot == "/cache/snapshot-a"
    load.assert_called_once_with("/cache/snapshot-a")


def test_knowledge_vectors_keep_their_snapshot_identity_during_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    from novacode_cli.tools import knowledge_tools

    installed = ["snapshot-a"]
    started, finish = Event(), Event()

    class Model:
        def __init__(self, snapshot: str | None) -> None:
            self.snapshot = snapshot

        def encode(self, _texts: list[str]) -> np.ndarray:
            if self.snapshot == "snapshot-a":
                started.set()
                assert finish.wait(2)
                return np.array([[1.0, 0.0]])
            return np.array([[0.0, 1.0]])

    monkeypatch.setattr(embeddings, "local_snapshot", lambda: installed[0])
    monkeypatch.setattr(
        embeddings, "_load", lambda snapshot: embeddings.SharedModel(Model(snapshot), snapshot)
    )
    with ThreadPoolExecutor(1) as consumers:
        first = consumers.submit(knowledge_tools._embed_with_identity, "query")
        try:
            assert started.wait(2)
            installed[0] = "snapshot-b"
            assert embeddings.get_model().snapshot == "snapshot-b"
        finally:
            finish.set()
        assert first.result(timeout=2) == ([1.0, 0.0], embeddings.MODEL_NAME + ":snapshot-a")
    assert knowledge_tools._embed_with_identity("query") == (
        [0.0, 1.0],
        embeddings.MODEL_NAME + ":snapshot-b",
    )


def test_snapshot_lookup_is_local_only_and_does_not_cache_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    download = Mock(side_effect=[OSError("absent"), "/cache/snapshot"])
    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    assert embeddings.local_snapshot() is None
    assert embeddings.local_snapshot() == "/cache/snapshot"
    assert download.call_count == 2
    download.assert_called_with(embeddings.MODEL_NAME, local_files_only=True)
