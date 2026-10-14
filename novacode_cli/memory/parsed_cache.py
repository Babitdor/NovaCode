"""Parsed memory cache with entry and resident-size limits."""

# JSON and LangGraph interfaces carry heterogeneous values.
# ruff: noqa: ANN401
import hashlib
import sys
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any


def file_hash(path: Any) -> str:
    """Hash current contents in bounded chunks regardless of timestamps."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resident_size(value: Any) -> int:
    """Count strings and token sets as well as the container holding them."""
    if isinstance(value, (tuple, list, set, frozenset)):
        return sys.getsizeof(value) + sum(resident_size(item) for item in value)
    return sys.getsizeof(value)


class ParsedMemoryCache(OrderedDict):
    """LRU; conservative accounting counts shared values for each entry."""

    def __init__(self, max_entries: int = 512, max_bytes: int = 16 * 1024 * 1024) -> None:
        """Initialize local state without acquiring invocation ownership."""
        super().__init__()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.resident_bytes = 0
        self._sizes: dict[Any, int] = {}

    def __setitem__(self, key: Any, value: Any) -> None:
        """Insert a parsed entry and evict the least recently used entries."""
        size = resident_size(key) + resident_size(value) + 256
        if key in self:
            self.resident_bytes -= self._sizes.pop(key)
            super().__delitem__(key)
        if size > self.max_bytes:
            return
        super().__setitem__(key, value)
        self._sizes[key] = size
        self.resident_bytes += size
        while len(self) > self.max_entries or self.resident_bytes > self.max_bytes:
            oldest, _ = super().popitem(last=False)
            self.resident_bytes -= self._sizes.pop(oldest)

    def __getitem__(self, key: Any) -> Any:
        """Read an entry and mark it as most recently used."""
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __delitem__(self, key: Any) -> None:
        """Delete a parsed entry and its resident-size accounting."""
        super().__delitem__(key)
        self.resident_bytes -= self._sizes.pop(key)


class MemoryCorpus(Mapping):
    """Keep discovery order while caching parsed bodies independently of corpus size."""

    def __init__(self, cache: ParsedMemoryCache, tokenize: Any) -> None:
        """Retain source descriptors; parse individual entries when retrieved."""
        self.cache, self.tokenize = cache, tokenize
        self.sources: dict[str, tuple[Any, Any, str]] = {}

    def add(self, topic: str, title: Any, source: Path | str, digest: str) -> None:
        """Register a source without admitting its body into the cache."""
        self.sources[topic] = (title, source, digest)

    def __len__(self) -> int:
        """Count discoverable topics, including uncached topics."""
        return len(self.sources)

    def __iter__(self) -> Iterator[str]:
        """Keep the original source ordering."""
        return iter(self.sources)

    def __getitem__(self, topic: str) -> Any:
        """Parse on demand and retain only bodies that fit the bounded LRU."""
        title, source, digest = self.sources[topic]
        key = (topic, str(source) if isinstance(source, Path) else "inline", digest)
        if key in self.cache:
            return self.cache[key]
        try:
            body = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        except OSError:
            # A file can disappear between discovery and retrieval. Do not
            # retain a negative parse; the next discovery sees the deletion.
            return title, "", frozenset()
        value = title, body, self.tokenize(body)
        self.cache[key] = value
        return value
