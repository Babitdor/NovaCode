"""Turn assistant markdown into speakable prose (the "prose only" TTS filter).

Reading code, tables, and URLs aloud is unpleasant, so before sending a reply to
TTS we strip everything that doesn't sound like natural speech and cap the length
so a long answer doesn't monologue. Pure and dependency-free → unit-tested in
isolation, and importable even when the audio extras aren't installed.
"""

from __future__ import annotations

import re

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # keep the label, drop the URL
_BARE_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]+)`")  # keep the word(s), drop the backticks
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUM_BULLET = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(.+?)\1")
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

_DEFAULT_MAX_CHARS = 1200
#: Cut at a sentence end within this many chars of the cap, if one exists.
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def speech_sentences(text: str) -> list[str]:
    """Split prose at sentence boundaries so playback need not await a whole reply."""
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


def speakable_text(markdown: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Reduce ``markdown`` to plain, speakable prose, truncated to ``max_chars``.

    Drops fenced code, images, tables, and bare URLs entirely; keeps link labels
    and inline-code words; removes markdown markup. Truncates at the nearest
    sentence boundary below the cap (falling back to an ellipsis).
    """
    if not markdown:
        return ""

    text = _FENCED_CODE.sub(" ", markdown)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _TABLE_ROW.sub(" ", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _NUM_BULLET.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = text.strip()

    if len(text) <= max_chars:
        return text
    return _truncate_to_sentence(text, max_chars)


def _truncate_to_sentence(text: str, max_chars: int) -> str:
    """Trim to the last sentence end at/under ``max_chars``, else ellipsize."""
    window = text[:max_chars]
    ends = list(_SENTENCE_END.finditer(window))
    if ends:
        return window[: ends[-1].end()].strip()
    return window.rstrip() + "…"


def is_all_code(markdown: str) -> bool:
    """Return whether nothing speakable remains after filtering (skip TTS)."""
    return not speakable_text(markdown).strip()
