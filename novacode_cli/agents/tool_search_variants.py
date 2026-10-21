"""Bounded regex matching and standalone BM25 ranking for tool discovery."""

from __future__ import annotations

import re
import time

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

REGEX_TOOL = "tool_search_tool_regex_20251119"
BM25_TOOL = "tool_search_tool_bm25_20251119"
SEARCH_VARIANTS = frozenset({REGEX_TOOL, BM25_TOOL})


class SearchInput(BaseModel):
    """Reject argument coercion even when the provider cannot constrain sampling."""

    model_config = ConfigDict(extra="forbid", strict=True)
    query: StrictStr
    limit: StrictInt = 5
    offset: StrictInt = 0


def regex_matches(catalog: list[dict], query: str) -> list[dict]:
    """Python regex syntax, case insensitive, with a total execution deadline."""
    import regex

    if len(query) > 200:
        raise ValueError("Regex patterns must not exceed 200 characters.")
    re.compile(query, re.IGNORECASE)  # Keep the accepted syntax compatible with Python re.
    pattern = regex.compile(query, regex.IGNORECASE)
    deadline = time.monotonic() + 0.2
    matches = []
    for item in catalog:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Regex search exceeded its execution deadline.")
        for text in (item["name"], item.get("description", ""), item.get("parameters", "")):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Regex search exceeded its execution deadline.")
            if pattern.search(text, timeout=min(remaining, 0.02)):
                matches.append(item)
                break
    return matches


class BM25Search:
    """A cached lexical index, independent of optional embeddings and downloads."""

    def __init__(self) -> None:
        self._cached = None

    def search(self, catalog: list[dict], query: str) -> list[dict]:
        import bm25s

        if len(query) > 500:
            raise ValueError("BM25 queries must not exceed 500 characters.")
        if not catalog or not re.search(r"\w", query):
            return []
        signature = tuple(
            (i["name"], i.get("description", ""), i.get("parameters", "")) for i in catalog
        )
        cached = self._cached
        if cached is not None and signature == cached[0]:
            index = cached[1]
        else:
            docs = [re.sub(r"[_-]", " ", " ".join(parts)) for parts in signature]
            index = bm25s.BM25()
            index.index(
                bm25s.tokenize(docs, stopwords="en", show_progress=False), show_progress=False
            )
            self._cached = (signature, index)
        tokens = bm25s.tokenize([re.sub(r"[_-]", " ", query)], stopwords="en", show_progress=False)
        if not any(tokens.ids):
            return []
        indices, scores = index.retrieve(tokens, k=len(catalog), show_progress=False)
        return [
            catalog[int(i)] for i, score in zip(indices[0], scores[0], strict=True) if score > 0
        ]


def parameter_text(tool) -> str:
    """Index argument names/descriptions without putting full schemas in prompts."""
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        spec = convert_to_openai_tool(tool)
        schema = spec.get("function", {}).get("parameters", {})
    except (TypeError, ValueError):
        return ""

    def walk(node):
        if not isinstance(node, dict):
            return []
        result = [str(node.get("description", ""))]
        for name, child in node.get("properties", {}).items():
            result.extend([name, *walk(child)])
        for name, child in node.get("$defs", {}).items():
            result.extend([name, *walk(child)])
        result.extend(walk(node.get("items")))
        for branch in node.get("anyOf", []):
            result.extend(walk(branch))
        return result

    return " ".join(walk(schema))
