"""Provider client isolation and request limits for delegated model calls."""

from typing import Any

# Bound one model turn, not the whole task: useful tool work can take much longer.
_MODEL_CALL_BUDGET = 240.0
_MODEL_REQUEST_TIMEOUT = 120.0


def _bounded_model(
    model: Any,
    *,
    request_timeout: float = _MODEL_REQUEST_TIMEOUT,
    max_retries: int | None = 0,
) -> Any:
    """Copy provider settings without mutating the shared/cached model.

    Delegated calls default to Nova-owned retries. Pass None to keep provider
    retries when the caller only supplies a wall-clock deadline.
    """
    fields = getattr(type(model), "model_fields", {})
    updates = {}
    if max_retries is not None and "max_retries" in fields:
        updates["max_retries"] = max_retries
    if "request_timeout" in fields:
        updates["request_timeout"] = request_timeout
    elif "default_request_timeout" in fields:
        updates["default_request_timeout"] = request_timeout
    elif "timeout" in fields:
        updates["timeout"] = request_timeout
    if not updates:
        return model
    bounded = model.model_copy(update=updates)
    # OpenAI-compatible models eagerly construct SDK clients. Updating the model
    # fields alone leaves those clients retrying with their original settings.
    for root_name, client_name in (
        ("root_client", "client"),
        ("root_async_client", "async_client"),
    ):
        root = getattr(model, root_name, None)
        if root is not None and hasattr(root, "with_options"):
            retry_options = {} if max_retries is None else {"max_retries": max_retries}
            root = root.with_options(timeout=request_timeout, **retry_options)
            setattr(bounded, root_name, root)
            setattr(bounded, client_name, root.chat.completions)
    # Anthropic constructs cached clients lazily; discard only the copy's cache.
    for name in ("_client", "_async_client", "_client_params"):
        bounded.__dict__.pop(name, None)
    return bounded
