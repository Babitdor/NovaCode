"""Provider client isolation and request limits for delegated model calls."""

from typing import Any

# Bound one model turn, not the whole task: useful tool work can take much longer.
_MODEL_CALL_BUDGET = 240.0
_MODEL_REQUEST_TIMEOUT = 120.0


def _bounded_model(model: Any) -> Any:
    """Copy provider settings without mutating the shared/cached model.

    Nova owns retries for delegated calls. Provider clients must not retry again
    underneath that policy. Custom models without these fields still receive the
    outer async deadline.
    """
    fields = getattr(type(model), "model_fields", {})
    updates = {}
    if "max_retries" in fields:
        updates["max_retries"] = 0
    if "request_timeout" in fields:
        updates["request_timeout"] = _MODEL_REQUEST_TIMEOUT
    elif "default_request_timeout" in fields:
        updates["default_request_timeout"] = _MODEL_REQUEST_TIMEOUT
    elif "timeout" in fields:
        updates["timeout"] = _MODEL_REQUEST_TIMEOUT
    if not updates:
        return model
    bounded = model.model_copy(update=updates)
    # OpenAI-compatible models eagerly construct SDK clients. Updating the model
    # fields alone leaves those clients retrying with their original settings.
    for root_name, client_name in (("root_client", "client"), ("root_async_client", "async_client")):
        root = getattr(model, root_name, None)
        if root is not None and hasattr(root, "with_options"):
            root = root.with_options(max_retries=0, timeout=_MODEL_REQUEST_TIMEOUT)
            setattr(bounded, root_name, root)
            setattr(bounded, client_name, root.chat.completions)
    # Anthropic constructs cached clients lazily; discard only the copy's cache.
    for name in ("_client", "_async_client", "_client_params"):
        bounded.__dict__.pop(name, None)
    return bounded

