"""Separate text estimates, fixed image heuristics and image transport size."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages.utils import count_tokens_approximately

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage

IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})
TOKENS_PER_IMAGE = 85


@dataclass(frozen=True)
class ContentEstimate:
    """Heuristics only; image dimensions and provider billing are unobserved."""

    text_tokens: int
    image_tokens: int
    image_transport_chars: int

    @property
    def total(self) -> int:
        """Input estimate excluding schemas, with no base64 character charge."""
        return self.text_tokens + self.image_tokens


def estimate_content(messages: Sequence[AnyMessage]) -> ContentEstimate:
    """Count images separately, including nested tool-result image blocks."""
    images = transport = 0

    def sanitize(value: Any) -> Any:  # noqa: ANN401 - heterogeneous content blocks
        nonlocal images, transport
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        if value.get("type") in IMAGE_TYPES:
            images += 1
            payload = value.get("base64") or value.get("image_url") or value.get("url")
            if isinstance(payload, Mapping):
                payload = payload.get("url") or payload.get("data")
            source = value.get("source")
            if not payload and isinstance(source, Mapping):
                payload = source.get("data")
            if isinstance(payload, str):
                if payload.startswith("data:"):
                    transport += len(payload.partition(",")[2])
                elif not payload.startswith(("https://", "http://")):
                    transport += len(payload)
            return {"type": "image"}
        return {key: sanitize(item) for key, item in value.items()}

    sanitized = [
        message.model_copy(update={"content": sanitize(message.content)})
        if isinstance(message.content, list)
        else message
        for message in messages
    ]
    text_tokens = count_tokens_approximately(sanitized, tokens_per_image=0)
    return ContentEstimate(text_tokens, images * TOKENS_PER_IMAGE, transport)
