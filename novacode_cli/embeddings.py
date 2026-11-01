"""One lazy local embedding model shared by retrieval and memory workers."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any

MODEL_NAME = "minishlab/potion-code-16M"
_FAILURE_BACKOFF = 2.0
_lock = threading.Lock()
_encode_lock = threading.Lock()
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_future: concurrent.futures.Future[SharedModel] | None = None
_snapshot: str | None = None
_retry_at = 0.0


def local_snapshot() -> str | None:
    """Inspect installed weights without caching absence or contacting the Hub."""
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(MODEL_NAME, local_files_only=True)
    except Exception:  # noqa: BLE001 - optional weights may be absent or corrupt.
        return None


class SharedModel:
    """Own serialized encoding for the process-wide snapshot."""

    def __init__(self, model: Any, snapshot: str | None) -> None:  # noqa: ANN401
        """Retain the loaded model and its immutable snapshot identity."""
        self.model = model
        self.snapshot = snapshot

    def encode(self, texts: list[str]) -> Any:  # noqa: ANN401
        """Encode on the calling worker, excluding concurrent model access."""
        with _encode_lock:
            return self.model.encode(texts)


def _load(snapshot: str | None) -> SharedModel:
    from model2vec import StaticModel

    if snapshot is None:
        from huggingface_hub import snapshot_download

        snapshot = snapshot_download(MODEL_NAME)
    return SharedModel(StaticModel.from_pretrained(snapshot), snapshot)


def prewarm() -> None:
    """Schedule snapshot inspection as well as model loading off the UI loop."""
    global _executor  # noqa: PLW0603
    with _lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="nova-embed")
        _executor.submit(get_model, allow_download=True, wait=False)


def get_model(*, allow_download: bool = False, wait: bool = True) -> SharedModel | None:
    """Coalesce worker loads; local readers never wait for an initial download.

    Skills may start a download. Other callers use lexical fallback until it
    completes. A changed snapshot bypasses the short failed-load backoff.
    """
    global _executor, _future, _snapshot, _retry_at  # noqa: PLW0603
    snapshot = local_snapshot()
    with _lock:
        future = _future
        # A first download discovers its identity only when it completes.
        # Harvest that identity before comparing it with the installed snapshot.
        if future is not None and future.done():
            try:
                _snapshot = future.result().snapshot
            except Exception:  # noqa: BLE001 - callers retain lexical fallback.
                future = _future = None
                _retry_at = time.monotonic() + _FAILURE_BACKOFF
        if future is not None and not future.done():
            if _snapshot is None or snapshot != _snapshot or not wait:
                return None
        elif snapshot != _snapshot or future is None:
            if (snapshot == _snapshot and time.monotonic() < _retry_at) or (
                snapshot is None and not allow_download
            ):
                return None
            if _executor is None:
                _executor = concurrent.futures.ThreadPoolExecutor(
                    1, thread_name_prefix="nova-embed"
                )
            _snapshot = snapshot
            future = _future = _executor.submit(_load, snapshot)
        if future is None or (not wait and not future.done()):
            return None
    try:
        result = future.result()
    except Exception:  # noqa: BLE001 - callers retain lexical fallback.
        with _lock:
            if _future is future:
                _future = None
                _retry_at = time.monotonic() + _FAILURE_BACKOFF
        return None
    installed = local_snapshot()
    with _lock:
        if _future is future:
            _snapshot = result.snapshot
        return result if _future is future and installed == result.snapshot else None
