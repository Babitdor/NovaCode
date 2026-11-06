"""Bounded repository-local facts with provenance, never execution authority."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from novacode_cli.prime_memory import (
    DEPENDENCIES,
    MAX_FILE_BYTES,
    project_context,
    safe_text,
    sanitized,
)

MAX_FILES = 256
MAX_BYTES = 256 * 1024
MAX_NODES = 2048
MAX_RECORD_BYTES = 32 * 1024
MAX_RESULTS = 20
MAX_KEY = 512
MAX_QUERY = 2000
MAX_SYMBOLS = 128
MAX_RETRIEVAL_BYTES = 16 * 1024
EXCLUDED = frozenset({".git", ".nova", ".venv", "node_modules", "__pycache__", ".aws", ".ssh"})
KINDS = frozenset(
    {"file", "symbol", "dependency", "task", "adr", "standard", "source", "finding", "check"}
)
Record = dict[str, Any]


class KnowledgeIndex:
    """Incremental Python AST facts in a versioned, bounded SQLite namespace."""

    def __init__(self, root: Path, directory: Path | None = None) -> None:
        """Keep runtime data in Git metadata without changing any candidate worktree."""
        self.root = Path(root).resolve()
        context = project_context(str(self.root))
        common = Path(context["common_dir"]).resolve() if context["common_dir"] else None
        boundaries = (self.root, common) if common is not None else (self.root,)
        if directory is None and common is not None:
            identity = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
            directory = common / "prime_factory" / "knowledge" / identity
        directory = Path(directory or self.root / ".nova" / "prime_factory").resolve()
        if not any(directory.is_relative_to(boundary) for boundary in boundaries):
            msg = "Knowledge storage must remain inside the repository or its Git common directory"
            raise ValueError(msg)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = (directory / "knowledge.sqlite3").resolve()
        if not any(self.path.is_relative_to(boundary) for boundary in boundaries):
            msg = "Knowledge storage must remain inside the repository or its Git common directory"
            raise ValueError(msg)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_v1 "
                "(kind TEXT, key TEXT, value TEXT NOT NULL, PRIMARY KEY(kind, key))"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _path(self, name: str) -> Path:
        candidate = (self.root / name).resolve()
        if not candidate.is_relative_to(self.root):
            msg = "Knowledge input escapes the repository"
            raise ValueError(msg)
        relative = candidate.relative_to(self.root)
        if any(part in EXCLUDED for part in relative.parts) or any(
            part.lower().startswith(".env")
            or part.lower() in {"credentials", "credentials.json", "secrets.json"}
            or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            for part in relative.parts
        ):
            msg = "Knowledge input is excluded"
            raise ValueError(msg)
        return candidate

    def fingerprint(self, name: str) -> str | None:
        """Read a bounded file; excluded or missing files cannot be fresh facts."""
        try:
            path = self._path(name)
            bound = MAX_FILE_BYTES if path.name in DEPENDENCIES else MAX_BYTES
            if path.stat().st_size > bound:
                return None
            with path.open("rb") as stream:
                content = stream.read(bound + 1)
            if len(content) <= bound:
                return hashlib.sha256(content).hexdigest()
        except (OSError, ValueError):
            return None
        return None

    def put(
        self,
        kind: str,
        key: str,
        content: Record,
        *,
        sources: tuple[str, ...] = (),
        links: tuple[tuple[str, str], ...] = (),
        fingerprints: dict[str, str | None] | None = None,
    ) -> Record:
        """Save a redacted fact with explicit relationships and source identities."""
        if kind not in KINDS or not key or len(key) > MAX_KEY:
            msg = "Invalid knowledge kind or key"
            raise ValueError(msg)
        inputs = fingerprints or {}
        for name in inputs:
            self._path(name)
        record = sanitized(
            {
                "version": 1,
                "kind": kind,
                "id": key,
                "content": content,
                "sources": list(sources),
                "links": [list(link) for link in links],
                "fingerprints": inputs,
                "untrusted": True,
            }
        )
        encoded = json.dumps(record, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > MAX_RECORD_BYTES:
            msg = "Knowledge record exceeds storage bound"
            raise ValueError(msg)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT COUNT(*) FROM knowledge_v1").fetchone()[0]
            exists = connection.execute(
                "SELECT 1 FROM knowledge_v1 WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
            if not exists and count >= MAX_NODES:
                msg = "Knowledge node limit reached"
                raise ValueError(msg)
            connection.execute(
                "INSERT INTO knowledge_v1 VALUES (?, ?, ?) "
                "ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value",
                (kind, key, encoded),
            )
        return record

    def records(self, kind: str) -> list[Record]:
        """Read a bounded deterministic set; unknown schemas never enter context."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT value FROM knowledge_v1 WHERE kind=? ORDER BY key LIMIT ?",
                (kind, MAX_NODES),
            ).fetchall()
        return [record for row in rows if (record := json.loads(row[0])).get("version") == 1]

    def current(self, record: Record) -> bool:
        """Invalidate facts when any supporting file disappears or changes."""
        return all(
            digest is not None and self.fingerprint(name) == digest
            for name, digest in record.get("fingerprints", {}).items()
        )

    def retrieve(self, query: str, limit: int = 5) -> list[Record]:
        """Rank exact token overlap, with deterministic ties and hash-based invalidation."""
        if not 1 <= limit <= MAX_RESULTS or len(query) > MAX_QUERY:
            msg = "Knowledge query exceeds retrieval bounds"
            raise ValueError(msg)
        tokens = set(re.findall(r"[\w.]+", query.lower()))
        matches = []
        for kind in sorted(KINDS):
            for record in self.records(kind):
                haystack = json.dumps([record["id"], record["content"]], sort_keys=True).lower()
                score = len(tokens & set(re.findall(r"[\w.]+", haystack)))
                if score and self.current(record):
                    matches.append((score, kind, record["id"], record))
        matches.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
        results = []
        remaining = MAX_RETRIEVAL_BYTES
        for entry in matches:
            record = entry[3]
            size = len(json.dumps(record).encode())
            if size <= remaining:
                results.append(record)
                remaining -= size
            if len(results) == limit:
                break
        return results

    async def update(self, paths: list[str]) -> Record:
        """Index explicitly selected paths away from the UI event loop."""
        if len(paths) > MAX_FILES:
            msg = "Knowledge update exceeds file bound"
            raise ValueError(msg)
        return await asyncio.to_thread(self._update, sorted(set(paths)))

    def _update(self, paths: list[str]) -> Record:
        coverage: Record = {"indexed": [], "unchanged": [], "unsupported": [], "unavailable": []}
        previous = {record["id"]: record for record in self.records("file")}
        for supplied in paths:
            path = self._path(supplied)
            name = path.relative_to(self.root).as_posix()
            digest = self.fingerprint(name)
            if digest is None:
                coverage["unavailable"].append(name)
                continue
            if name in previous and previous[name]["fingerprints"] == {name: digest}:
                coverage["unchanged"].append(name)
                continue
            structure = "python_ast" if path.suffix == ".py" else "unsupported"
            symbols = []
            if structure == "python_ast":
                try:
                    tree = ast.parse(path.read_bytes(), filename=name)
                except (SyntaxError, ValueError):
                    structure = "invalid_python"
                else:
                    symbols = [
                        (node.name, node.lineno, type(node).__name__)
                        for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                    ]
                    if len(symbols) > MAX_SYMBOLS:
                        structure = "python_ast_truncated"
                    symbols = symbols[:MAX_SYMBOLS]
            if structure != "python_ast":
                coverage["unsupported"].append(name)
            # Remove old symbol identities when functions are renamed or deleted.
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM knowledge_v1 WHERE kind='symbol' AND key LIKE ? ESCAPE '\\'",
                    (name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%",),
                )
            self.put(
                "file",
                name,
                {"path": name, "structural_coverage": structure},
                fingerprints={name: digest},
            )
            for symbol, line, node_kind in symbols:
                self.put(
                    "symbol",
                    f"{name}:{line}:{symbol}",
                    {"name": safe_text(symbol), "line": line, "node_kind": node_kind},
                    sources=(name,),
                    links=(("defined_in", name),),
                    fingerprints={name: digest},
                )
            coverage["indexed"].append(name)
        return coverage
