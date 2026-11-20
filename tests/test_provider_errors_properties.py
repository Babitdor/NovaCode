"""Provider-error classification: the boundaries, and its totality under fuzzing.

``tests/test_provider_errors.py`` pins the real-world messages. This file covers
the remaining branches — a status that arrives as text, a 5xx, a missing
exception — and then stops enumerating examples: a property-based layer asserts
what must be true for *any* provider failure, because the classifier runs inside
the error funnel where raising would replace the user's real error with ours.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from novacode_cli.errors import (
    friendly_model_error,
    is_context_overflow,
    is_retryable_model_error,
)
from novacode_cli.errors.provider_errors import _CAP_MARKERS, _PERMANENT


class _ProviderError(Exception):
    """Stand-in for an SDK error carrying a status the way real ones do."""

    def __init__(self, message: str, status: object = None) -> None:
        super().__init__(message)
        self.status = status


def test_a_status_that_arrives_as_text_is_read_as_a_code() -> None:
    """Several SDKs stringify the status; refusing it would lose the notice.

    The message carries no marker of its own, so only the text status can
    classify this — a classifier that ignored "429" would return no notice.
    """
    error = _ProviderError("the request was rejected", "429")

    notice = friendly_model_error(error)

    assert notice is not None
    assert "rate limit reached" in notice.lower()
    assert is_retryable_model_error(error) is True


def test_a_status_that_is_neither_int_nor_digits_is_ignored() -> None:
    error = _ProviderError("some unrelated bug", "not-a-status")

    assert friendly_model_error(error) is None


def test_a_5xx_is_a_server_notice_and_stays_retryable() -> None:
    """A transient upstream failure should back off, not fail the turn."""
    error = _ProviderError("upstream exploded", 503)

    notice = friendly_model_error(error)

    assert notice is not None
    assert "temporarily unavailable" in notice.lower()
    assert is_retryable_model_error(error) is True


@pytest.mark.parametrize(
    "marker",
    ["server_error", "endpoint is unavailable", "upstream request failed"],
)
def test_server_markers_classify_without_a_status(marker: str) -> None:
    """Providers word a 5xx in the body; the status code is not always readable."""
    notice = friendly_model_error(_ProviderError(f"provider said {marker}"))

    assert notice is not None
    assert "temporarily unavailable" in notice.lower()


def test_no_exception_is_not_a_context_overflow() -> None:
    assert is_context_overflow(None) is False


def test_a_token_count_mention_alone_is_not_an_overflow() -> None:
    """A false positive here compacts the user's conversation for no reason."""
    assert is_context_overflow(ValueError("max_tokens must be positive")) is False


def test_an_overflow_is_never_retried() -> None:
    """Backoff reproduces the same failure; the caller has to compact instead."""
    error = ValueError("This model's maximum context length is 8192 tokens")

    assert is_context_overflow(error) is True
    assert is_retryable_model_error(error) is False


# --- properties that must hold for any provider failure ---------------------

_TEXT = st.text(max_size=200)


@given(message=_TEXT, status=st.none() | st.integers(100, 599) | st.sampled_from(["429", "401"]))
@settings(max_examples=150, deadline=None)
def test_classification_never_raises_and_any_notice_is_usable(message: str, status: object) -> None:
    """The funnel calls both helpers on every failure; neither may raise."""
    error = _ProviderError(message, status)

    notice = friendly_model_error(error)
    retryable = is_retryable_model_error(error)
    overflow = is_context_overflow(error)

    assert isinstance(retryable, bool)
    assert isinstance(overflow, bool)
    assert notice is None or (isinstance(notice, str) and notice.strip())


@given(message=_TEXT)
@settings(max_examples=150, deadline=None)
def test_a_notice_keeps_the_providers_own_words(message: str) -> None:
    """Nothing actionable may be dropped: the detail line carries the original text."""
    error = _ProviderError(f"Error code: 429 - {message}")

    notice = friendly_model_error(error)

    assert notice is not None
    assert f"Details: {str(error).strip()}" in notice


@given(message=_TEXT, status=st.none() | st.integers(100, 599))
@settings(max_examples=150, deadline=None)
def test_an_overflow_is_never_retried_whatever_the_status(message: str, status: object) -> None:
    """Compaction is the only recovery; a retry would burn the backoff and fail again."""
    error = _ProviderError(f"prompt is too long: {message}", status)

    assert is_context_overflow(error) is True
    assert is_retryable_model_error(error) is False


@given(text=st.from_regex(r"[a-z0-9 .:\-]{0,60}", fullmatch=True))
@settings(max_examples=150, deadline=None)
def test_a_throttling_429_backs_off_unless_it_names_a_real_cap(text: str) -> None:
    """Every throttling provider 429s; only cap wording is permanent."""
    assume(not any(marker in text.lower() for marker in _CAP_MARKERS))
    assume("quota" not in text.lower() and "billing" not in text.lower())
    error = _ProviderError(text, 429)

    assert is_retryable_model_error(error) is True
    assert friendly_model_error(error) is not None


@given(category=st.sampled_from(sorted(_PERMANENT)))
@settings(max_examples=20, deadline=None)
def test_every_permanent_category_is_never_retried(category: str) -> None:
    """_PERMANENT is the contract the retry predicate reads; keep them in step."""
    phrase = {
        "usage_limit": "you have reached your weekly usage limit",
        "auth": "unauthorized: invalid api key",
        "region": "regionerror: only available hosted in China",
        "plan": "upgrade_required: this plan has no API access",
        "zdr": "cmd_zdr_no_providers",
    }[category]

    assert is_retryable_model_error(_ProviderError(phrase, 400)) is False
