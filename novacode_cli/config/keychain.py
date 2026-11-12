"""Bound reads from OS keychains that may still be locked during system startup."""

from __future__ import annotations

import logging
import threading
from queue import Queue
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")
_READ_TIMEOUT = 3.0
_pending = threading.Lock()
logger = logging.getLogger(__name__)


def keychain_read(operation: Callable[[], _T]) -> _T:
    """Wait briefly for a read without letting a stuck keychain prevent exit.

    Only one read may be outstanding. Timed-out work never changes credentials
    or the environment, and subsequent reads fail promptly until it finishes.
    Writes deliberately stay synchronous: a timeout must not imply a write failed
    while it can still complete later.
    """
    pending = _pending
    if not pending.acquire(timeout=_READ_TIMEOUT):
        msg = "OS keychain is busy; unlock it or set the provider key in the environment."
        raise TimeoutError(msg)
    results: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def read() -> None:
        try:
            try:
                results.put((True, operation()))
            except BaseException as exc:  # noqa: BLE001 - propagate worker failures to its caller
                results.put((False, exc))
        finally:
            pending.release()

    worker = threading.Thread(target=read, name="nova-keychain-read", daemon=True)
    try:
        worker.start()
    except BaseException:
        pending.release()
        raise
    worker.join(_READ_TIMEOUT)
    if worker.is_alive():
        msg = "OS keychain did not respond; unlock it or set the provider key in the environment."
        logger.warning(msg)
        raise TimeoutError(msg)
    success, value = results.get_nowait()
    if not success:
        raise cast("BaseException", value)
    return cast("_T", value)
