"""Shared utilities and constants for tools modules.

This module provides common imports, constants, and session management
shared across all tool modules.
"""

from __future__ import annotations

import atexit
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random
import threading
from secrets import SystemRandom

import requests
from requests.adapters import HTTPAdapter

# Secure random generator for user agent rotation
_secure_random = SystemRandom()

# Common browser user agents for rotation
_BROWSER_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.2 Safari/605.1.15"
    ),
]

# Shared session-level connection pool for all HTTP requests
_http_session: requests.Session | None = None
_http_session_lock = threading.Lock()
_MAX_RETRY_AFTER_SECONDS = 60.0


def _retry_delay(attempt: int, retry_after: str | None = None) -> float | None:
    """Return a bounded jittered delay, or None when Retry-After is too long."""
    delay = min(2.0 ** (attempt + 1), 8.0)
    if retry_after:
        try:
            requested = max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                requested = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                requested = 0.0
        if not math.isfinite(requested) or requested > _MAX_RETRY_AFTER_SECONDS:
            return None
        delay = max(delay, requested)
    return delay + random.uniform(0.0, min(0.5, delay * 0.1))


def _get_http_session() -> requests.Session:
    """Get or create a reusable requests session with connection pooling."""
    global _http_session
    if _http_session is not None:
        return _http_session
    with _http_session_lock:
        if _http_session is None:
            session = requests.Session()
            # Keep connection pooling, but leave retries to callers. Hidden
            # adapter retries multiply tool-level attempts and ignore their
            # Retry-After, deadline, and cancellation policies.
            adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            _http_session = session
        return _http_session


def _close_http_session() -> None:
    global _http_session
    with _http_session_lock:
        session, _http_session = _http_session, None
    if session is not None:
        session.close()


def _get_fetch_session() -> requests.Session:
    """Alias for _get_http_session — unified connection pool."""
    return _get_http_session()


atexit.register(_close_http_session)
