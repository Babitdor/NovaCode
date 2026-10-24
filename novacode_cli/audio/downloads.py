"""Model download events shared by voice providers and the TUI."""
# ruff: noqa: ANN401 -- Hugging Face/tqdm forward heterogeneous public arguments.

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

logger = logging.getLogger("nova.audio.downloads")
_OPERATIONS = count()
_PROGRESS_INTERVAL_S = 0.15


@dataclass(frozen=True)
class ModelDownload:
    """One operation's current model preparation or file-transfer state."""

    operation_id: str
    kind: str
    provider: str
    state: str
    filename: str = ""
    completed: int = 0
    total: int | None = None
    error: str = ""
    sequence: int = field(default_factory=lambda: next(_OPERATIONS))


DownloadCallback = Callable[[ModelDownload], None]
_CALLBACK: ContextVar[DownloadCallback | None] = ContextVar("voice_download_callback", default=None)
_CURRENT: ContextVar[_Reporter | None] = ContextVar("voice_model_download", default=None)
_HOOK_LOCK = threading.Lock()


class _Reporter:
    def __init__(self, kind: str, provider: str, callback: DownloadCallback) -> None:
        self.event = ModelDownload(uuid4().hex, kind, provider, "loading")
        self.callback = callback
        self.lock = threading.RLock()
        self.closed = False
        self.visible = False
        self.last_update = 0.0

    def emit(
        self,
        state: str,
        *,
        filename: str = "",
        completed: int = 0,
        total: int | None = None,
        error: str = "",
        force: bool = False,
    ) -> None:
        with self.lock:
            if self.closed:
                return
            now = time.monotonic()
            if (
                state == "downloading"
                and not force
                and self.event.state == state
                and filename == self.event.filename
                and completed != total
                and now - self.last_update < _PROGRESS_INTERVAL_S
            ):
                return
            self.event = replace(
                self.event,
                state=state,
                filename=filename,
                completed=max(0, completed),
                total=total,
                error=error,
            )
            self.visible = True
            self.last_update = now
            try:
                self.callback(self.event)
            except Exception:  # noqa: BLE001 -- a closed UI must not break a model download.
                logger.debug("Voice progress callback failed", exc_info=True)

    def finish(self, state: str, error: str = "") -> None:
        with self.lock:
            if self.visible:
                self.emit(
                    state,
                    filename=self.event.filename,
                    completed=self.event.completed,
                    total=self.event.total,
                    error=error,
                )
            self.closed = True

    def progress_class(self) -> type[Any]:
        from tqdm.auto import tqdm

        reporter = self

        class QuietStream(io.StringIO):
            def write(self, text: str) -> int:
                return len(text)

        # tqdm.auto is dynamically selected and has no bundled typing.
        class ModelTqdm(tqdm):  # type: ignore[misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.update(file=QuietStream(), disable=False, leave=False)
                super().__init__(*args, **kwargs)
                self._report()

            def _report(self, *, force: bool = False) -> None:
                if getattr(self, "unit", None) != "B":
                    return
                total = getattr(self, "total", None)
                completed = int(getattr(self, "n", 0))
                if completed or total:
                    reporter.emit(
                        "downloading",
                        filename=str(getattr(self, "desc", "model")),
                        completed=completed,
                        total=int(total) if total else None,
                        force=force,
                    )

            def update(self, n: float = 1) -> bool | None:
                result = super().update(n)
                self._report()
                return result

            def refresh(self, *args: Any, **kwargs: Any) -> bool | None:
                result = super().refresh(*args, **kwargs)
                self._report()
                return result

            def close(self) -> None:
                self._report(force=True)
                super().close()

        return ModelTqdm


def _wrap_hub_download(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def download(*args: Any, **kwargs: Any) -> Any:
        reporter = _CURRENT.get()
        if reporter is not None:
            kwargs["tqdm_class"] = reporter.progress_class()
        return original(*args, **kwargs)

    download._nova_voice_progress = True  # type: ignore[attr-defined]
    return download


def _install_hub_progress() -> None:
    """Adapt public HF progress hooks; calls outside a voice scope are unchanged."""
    try:
        import huggingface_hub
    except ImportError:
        return
    with _HOOK_LOCK:
        for name in ("hf_hub_download", "snapshot_download"):
            original = getattr(huggingface_hub, name, None)
            if original is None or getattr(original, "_nova_voice_progress", False):
                continue
            parameters = inspect.signature(original).parameters
            if "tqdm_class" not in parameters and not any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
            ):
                continue  # Older HF versions can still show indeterminate preparation.
            setattr(huggingface_hub, name, _wrap_hub_download(original))


@contextmanager
def download_progress(callback: DownloadCallback) -> Iterator[None]:
    """Bind progress for pipelines created inside a captured TUI command."""
    token = _CALLBACK.set(callback)
    try:
        yield
    finally:
        _CALLBACK.reset(token)


def report_model_loading() -> None:
    """Leave byte progress behind while native initialization is still running."""
    reporter = _CURRENT.get()
    if reporter is not None and reporter.visible:
        reporter.emit("loading")


@asynccontextmanager
async def watch_downloads(
    kind: str, provider: str, backend: Any, callback: DownloadCallback | None
) -> AsyncIterator[None]:
    """Report one load; copied thread contexts retain a request-local reporter."""
    callback = callback or _CALLBACK.get()
    if callback is None:
        yield
        return
    await asyncio.to_thread(_install_hub_progress)
    reporter = _Reporter(kind, provider, callback)
    token = _CURRENT.set(reporter)
    try:
        try:
            pending = await asyncio.to_thread(
                lambda: getattr(
                    backend, "needs_download", getattr(backend, "_model", object()) is None
                )
            )
        except Exception:  # noqa: BLE001 -- cache detection is only a progress hint.
            pending = False
        if pending:
            reporter.emit("loading")
        yield
    except asyncio.CancelledError:
        reporter.finish("cancelled")
        raise
    except Exception as exc:
        reporter.finish("error", str(exc))
        raise
    else:
        reporter.finish("ready")
    finally:
        reporter.closed = True
        _CURRENT.reset(token)


def download_file(url: str, dest: Path) -> None:
    """Stream an owned voice asset with byte progress and atomic replacement."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    reporter = _CURRENT.get()
    completed = 0
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 -- callers supply fixed model hosts.
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() and int(length) > 0 else None
            if reporter:
                reporter.emit("downloading", filename=dest.name, total=total)
            while chunk := response.read(64 * 1024):
                output.write(chunk)
                completed += len(chunk)
                if reporter:
                    reporter.emit(
                        "downloading", filename=dest.name, completed=completed, total=total
                    )
            if total is not None and completed != total:
                msg = f"Incomplete download for {dest.name}: {completed} of {total} bytes"
                raise OSError(msg)
            if reporter:
                reporter.emit(
                    "downloading", filename=dest.name, completed=completed, total=total, force=True
                )
        Path(temporary).replace(dest)
    finally:
        Path(temporary).unlink(missing_ok=True)
