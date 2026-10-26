"""Extract provider-exposed reasoning text, never opaque signatures or encrypted data."""

from __future__ import annotations


def _visible_text(block: object) -> str:  # noqa: PLR0911 -- explicit provider text shapes.
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type", "")
    if kind in {"redacted_thinking", "reasoning.encrypted"}:
        return ""
    for field in ("text", "reasoning", "thinking"):
        text = block.get(field)
        if isinstance(text, str) and text:
            return text
        if field == "thinking" and isinstance(text, list):
            return "".join(_visible_text(part) for part in text)
    summary = block.get("summary")
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        return "".join(_visible_text(part) for part in summary)
    for field in ("reasoningText", "reasoning_content", "reasoningContent"):
        reasoning = block.get(field)
        if isinstance(reasoning, (dict, str)):
            return _visible_text(reasoning)
    return ""


def reasoning_text(message: object, blocks: list[dict]) -> str:
    """Prefer standardized blocks; use known provider fields only as a fallback.

    Different SDKs normalize the same response into different fields. Selecting
    one representation avoids displaying both a normalized thought and its raw
    metadata copy. The caller owns streaming/completed-message deduplication.
    """
    parts: list[str] = []
    native_parts: list[str] = []
    for block in blocks:
        value = block.get("value") if block.get("type") == "non_standard" else block
        if isinstance(value, dict) and (
            value.get("type") in {"reasoning", "thinking", "reasoning_content"}
            or (value.get("type") == "text" and value.get("thought") is True)
        ):
            target = parts if block.get("type") == "reasoning" else native_parts
            target.append(_visible_text(value))
    normalized = "".join(parts) or "".join(native_parts)
    if normalized:
        return normalized
    extra = getattr(message, "additional_kwargs", None)
    if not isinstance(extra, dict):
        return ""
    for field in ("reasoning_content", "reasoning"):
        text = _visible_text(extra.get(field))
        if text:
            return text
    details = extra.get("reasoning_details")
    if isinstance(details, list):
        return "".join(
            _visible_text(detail)
            for detail in details
            if isinstance(detail, dict)
            and detail.get("type") in {"reasoning.text", "reasoning.summary"}
        )
    return ""
