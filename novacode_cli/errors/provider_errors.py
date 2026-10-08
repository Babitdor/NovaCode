"""Friendly formatting and retry classification for model-provider failures.

When a model call fails because the provider rejected it — a usage/quota cap, a
429 rate limit, a bad API key, or an unreachable endpoint — the raw exception is
a noisy SDK traceback (e.g. ``ollama._types.ResponseError`` or an
``openai.RateLimitError``). Surfacing that verbatim is unhelpful and, in the
Textual TUI, visually jarring.

Two helpers share one classifier:

* :func:`friendly_model_error` turns a known failure into a concise, actionable
  notice. It is called from the single error funnel in
  :func:`novacode_cli.core.agent_loop.iterate_agent_events`, so both front-ends
  (TUI + legacy REPL) render the same clean message.
* :func:`is_retryable_model_error` is the ``retry_on`` predicate for
  ``ModelRetryMiddleware``: it skips retries on *permanent* failures (a weekly
  usage cap or a bad key won't fix itself within a few seconds of backoff) so
  the clean error surfaces immediately instead of after several pointless waits.

Both return falsy/``None`` for anything unrecognised, so unknown errors keep the
default behaviour (retry, then ``str(exc)``).
"""

from __future__ import annotations

_HTTP_RATE_LIMITED = 429
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403

# Category -> user-facing notice template (``{text}`` = provider's own message,
# which usually carries the upgrade/settings URLs, kept so nothing is lost).
_NOTICES = {
    "usage_limit": (
        "⚠️  Model provider usage limit reached\n"
        "You've hit your provider's usage/quota limit, so the request was rejected. "
        "Upgrade your plan or add usage with your provider, then try again.\n"
        "Details: {text}"
    ),
    "rate_limit": (
        "⚠️  Rate limit reached\n"
        "The model provider is throttling requests (HTTP 429). Wait a moment and retry, "
        "or raise your provider limits.\n"
        "Details: {text}"
    ),
    "auth": (
        "⚠️  Authentication error\n"
        "The provider rejected your credentials — check your API key or subscription.\n"
        "Details: {text}"
    ),
    "region": (
        "🌏  Model not available in your region\n"
        "This model is region-restricted and needs an explicit opt-in on your provider "
        "account (your key is fine). Enable it via the link in the details, or switch to a "
        "model that doesn't require opt-in.\n"
        "Details: {text}"
    ),
    "connection": (
        "⚠️  Could not reach the model provider\n"
        "Check your network, or that the local model server (e.g. Ollama) is running.\n"
        "Details: {text}"
    ),
    "server": (
        "⚠️  Model provider temporarily unavailable\n"
        "The provider failed to serve the request after retries. Try again or switch models.\n"
        "Details: {text}"
    ),
}

# Categories that won't recover within a retry window, so don't waste backoff.
# "region" needs a user opt-in on the provider account, so retrying is pointless.
_PERMANENT = frozenset({"usage_limit", "auth", "region"})


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an SDK exception (attr varies by library)."""
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _classify(exc: BaseException | None) -> str | None:
    """Bucket a provider failure into a category, or ``None`` if unrecognised.

    Priority matters: a weekly usage cap is often *also* an HTTP 429, so the
    usage-limit check must run before the generic rate-limit one.
    """
    if exc is None:
        return None

    low = str(exc).strip().lower()
    status = _status_code(exc)

    # Usage / quota / credit caps — most specific. Ollama Cloud's weekly cap and
    # OpenAI/Anthropic "insufficient_quota" all land here.
    if (
        "usage limit" in low
        or "quota" in low
        or "weekly limit" in low
        or "out of credit" in low
        or "billing" in low
        or ("insufficient" in low and "credit" in low)
    ):
        return "usage_limit"

    # Rate limiting (transient) — HTTP 429 / "too many requests".
    if (
        status == _HTTP_RATE_LIMITED
        or "rate limit" in low
        or "too many requests" in low
        or "429" in low
    ):
        return "rate_limit"

    # Region / opt-in restriction — a 403 that is NOT a credentials problem
    # (e.g. OpenCode: "RegionError … only available hosted in China and requires
    # explicit opt in"). Must run BEFORE the auth check so it isn't mislabelled
    # "check your API key".
    if (
        "regionerror" in low
        or "only available hosted in" in low
        or ("region" in low and ("opt in" in low or "opt-in" in low))
    ):
        return "region"

    # Authentication / authorization.
    if (
        status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN)
        or "unauthorized" in low
        or "forbidden" in low
        or "invalid api key" in low
        or "incorrect api key" in low
        or "authentication" in low
    ):
        return "auth"

    # Connectivity — common with a local Ollama server that isn't running.
    if any(
        k in low
        for k in (
            "connection refused",
            "could not connect",
            "connection error",
            "failed to establish",
            "max retries exceeded",
            "name or service not known",
            "connection aborted",
        )
    ):
        return "connection"

    if (status is not None and 500 <= status < 600) or any(
        marker in low
        for marker in ("server_error", "endpoint is unavailable", "upstream request failed")
    ):
        return "server"

    return None


def friendly_model_error(exc: BaseException | None) -> str | None:
    """Return a clean, user-facing notice for a known provider error, else None.

    Recognises usage/quota limits, rate limits (HTTP 429), authentication
    failures (401/403), and connectivity problems. The provider's own message —
    which usually carries the upgrade/settings URLs — is preserved on a
    ``Details:`` line so nothing actionable is lost.
    """
    category = _classify(exc)
    if category is None:
        return None
    return _NOTICES[category].format(text=str(exc).strip())


#: Phrases providers use when the request exceeded the model's context window.
#: Deliberately specific: a generic "too long" or a bare "context" would also
#: match unrelated failures, and a false positive here triggers a needless
#: compaction of the user's conversation.
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context length",
    "context window",
    "context_length_exceeded",
    "maximum context",
    "max_tokens",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length of the messages",
    "exceeds the maximum",
    "request too large",
)


def is_context_overflow(exc: BaseException | None) -> bool:
    """True if *exc* is the provider rejecting a request for being too long.

    Distinct from :func:`is_retryable_model_error`: an overflow *is* recoverable,
    but only by shrinking the conversation (compact, then retry) — plain backoff
    would fail identically every time. The caller emits a dedicated event so the
    TUI can compact and retry once instead of surfacing a dead-end error.
    """
    if exc is None:
        return False
    low = str(exc).strip().lower()
    # A token-count phrase alone is ambiguous ("max_tokens must be positive"),
    # so require it to read like a limit being exceeded.
    if any(marker in low for marker in _OVERFLOW_MARKERS):
        return any(
            word in low
            for word in ("exceed", "too long", "too large", "too many", "maximum", "limit")
        )
    return False


def is_retryable_model_error(exc: BaseException) -> bool:
    """``retry_on`` predicate for ``ModelRetryMiddleware``.

    Returns ``False`` for permanent provider failures and context overflows
    (which need compaction rather than backoff); everything else — transient
    rate limits, timeouts, network blips, and unrecognised errors — stays
    retryable, preserving the prior retry-on-all-exceptions behaviour.
    """
    return not is_context_overflow(exc) and _classify(exc) not in _PERMANENT
