"""Tests for friendly formatting of model-provider failures.

``friendly_model_error`` turns noisy SDK exceptions (Ollama / OpenAI / Anthropic
rate-limit, quota, auth, and connection errors) into a clean, actionable notice
that both front-ends render identically. See
:mod:`novacode_cli.errors.provider_errors`.
"""

from __future__ import annotations

from novacode_cli.errors import (
    friendly_model_error,
    is_context_overflow,
    is_retryable_model_error,
)


def test_provider_413_request_entity_too_large_needs_compaction() -> None:
    error = _ResponseError("Error code: 413 - Request Entity Too Large", 413)
    assert is_context_overflow(error)
    assert not is_retryable_model_error(error)


class _ResponseError(Exception):
    """Stand-in mirroring ollama._types.ResponseError (str + status_code)."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self._message = message
        self.status_code = status_code

    def __str__(self) -> str:
        return self._message


# The exact error the user hit: Ollama Cloud weekly usage cap (HTTP 429).
_OLLAMA_USAGE_LIMIT = (
    "you (Babitdor) have reached your weekly usage limit, upgrade for higher "
    "limits: https://ollama.com/upgrade or add extra usage: "
    "https://ollama.com/settings (ref: 145a063a) (status code: 429)"
)


def test_ollama_weekly_usage_limit_is_usage_notice():
    notice = friendly_model_error(_ResponseError(_OLLAMA_USAGE_LIMIT, 429))
    assert notice is not None
    assert "usage limit reached" in notice.lower()
    # The provider's own message (with the upgrade URLs) is preserved verbatim.
    assert "https://ollama.com/upgrade" in notice
    assert "https://ollama.com/settings" in notice


# OpenCode DeepSeek V4: a 403 that is a region opt-in requirement, NOT bad creds.
_OPENCODE_REGION = (
    "Error code: 403 - {'type': 'error', 'error': {'type': 'RegionError', 'message': "
    "'The latest version of this model is only available hosted in China and requires "
    "explicit opt in: https://opencode.ai/workspace/wrk_01M0/go'}}"
)


def test_region_error_is_not_mislabelled_as_auth():
    notice = friendly_model_error(_ResponseError(_OPENCODE_REGION, 403))
    assert notice is not None
    assert "region" in notice.lower()
    # Must NOT tell the user to check their API key — the key is fine.
    assert "API key" not in notice
    # The opt-in link is preserved so the user can act on it.
    assert "opencode.ai/workspace" in notice


def test_region_error_is_not_retried():
    # Region opt-in needs a user action; retrying wastes backoff.
    assert is_retryable_model_error(_ResponseError(_OPENCODE_REGION, 403)) is False


def test_plain_403_still_classified_as_auth():
    notice = friendly_model_error(_ResponseError("403 Forbidden: invalid api key", 403))
    assert notice is not None and "authentication" in notice.lower()


def test_usage_limit_takes_priority_over_429_rate_limit():
    # The message contains both "usage limit" and "429"; the usage notice wins.
    notice = friendly_model_error(_ResponseError(_OLLAMA_USAGE_LIMIT, 429))
    assert "usage limit" in notice.lower()
    assert "throttling" not in notice.lower()  # not the generic rate-limit copy


def test_plain_429_is_rate_limit_notice():
    notice = friendly_model_error(_ResponseError("Too Many Requests", 429))
    assert notice is not None
    assert "rate limit reached" in notice.lower()


def test_auth_401_is_auth_notice():
    notice = friendly_model_error(_ResponseError("Unauthorized: invalid api key", 401))
    assert notice is not None
    assert "authentication error" in notice.lower()


def test_connection_error_is_connectivity_notice():
    notice = friendly_model_error(
        ConnectionError("Max retries exceeded: could not connect to localhost:11434")
    )
    assert notice is not None
    assert "could not reach" in notice.lower()


def test_unrecognized_error_returns_none():
    assert friendly_model_error(ValueError("some unrelated bug")) is None


def test_none_returns_none():
    assert friendly_model_error(None) is None


def test_message_is_multiline_and_keeps_details():
    notice = friendly_model_error(_ResponseError(_OLLAMA_USAGE_LIMIT, 429))
    lines = notice.splitlines()
    assert len(lines) >= 2
    assert any(line.startswith("Details:") for line in lines)


# --- retry predicate (ModelRetryMiddleware retry_on) -----------------------


def test_usage_limit_is_not_retryable():
    # A weekly cap won't clear within a backoff window — fail fast.
    assert is_retryable_model_error(_ResponseError(_OLLAMA_USAGE_LIMIT, 429)) is False


def test_auth_is_not_retryable():
    assert is_retryable_model_error(_ResponseError("Unauthorized: invalid api key", 401)) is False


def test_plain_rate_limit_is_retryable():
    # Transient throttling can clear with backoff, so keep retrying.
    assert is_retryable_model_error(_ResponseError("Too Many Requests", 429)) is True


def test_connection_is_retryable():
    assert is_retryable_model_error(ConnectionError("could not connect")) is True


def test_unknown_error_stays_retryable():
    # Preserve the prior retry-on-all-exceptions default for unrecognised errors.
    assert is_retryable_model_error(ValueError("some unrelated bug")) is True
