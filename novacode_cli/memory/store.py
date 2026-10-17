"""Durable, process-lifetime LangGraph store for cross-session agent memory.

Nova's filesystem ``/memories/`` route already persists markdown memory on disk.
This module adds the *other* half the deep-agents memory model expects: a durable
LangGraph ``BaseStore`` for **structured, key/value, cross-session** memory
(``put`` / ``get`` / ``search``).

A single sync :class:`~langgraph.store.sqlite.SqliteStore` at ``~/.nova/store.db``
backs everything. Sync callers (the ``remember`` / ``recall`` / ``forget`` tools)
use it directly; async callers (e.g. ``NovaLearningMiddleware``, which calls
``aput`` / ``aget``) get the *same* store via :class:`DualModeStore`, whose async
methods run the sync store in a worker thread (``asyncio.to_thread``) under a
lock.

Why not ``AsyncSqliteStore``? ``SqliteStore.abatch`` raises ``NotImplementedError``
(so its ``aput`` / ``aget`` fail), and ``AsyncSqliteStore`` pulls in ``aiosqlite``
plus an event-loop-bound, non-daemon worker thread (causing import failures in
some environments and hangs on exit). Delegating async calls to the sync store in
a thread keeps one durable DB, needs no extra dependency, and exits cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from novacode_cli.config.config import HOME_DIR, boot_status

# BaseStore lives in core langgraph (always installed) — unlike SqliteStore,
# which comes from the optional langgraph-checkpoint-sqlite package.
from langgraph.store.base import BaseStore

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langgraph.store.base import Item


# ═══════════════════════════════════════════════════════════════════════════
# Stdlib-sqlite3 durable store — fallback that needs NO extra package
# ═══════════════════════════════════════════════════════════════════════════


class _StdlibSqliteStore(BaseStore):
    """A minimal durable ``BaseStore`` backed only by the stdlib ``sqlite3``.

    Used when the optional ``langgraph-checkpoint-sqlite`` package (which
    provides ``langgraph.store.sqlite.SqliteStore``) isn't installed — so Nova
    keeps durable cross-session memory instead of silently degrading to
    in-memory. Implements the key/value + namespace-prefix-search subset Nova
    and deep-agents use; no vector/semantic index or TTL.
    """

    _SEP = "\x1f"  # unit separator — joins namespace tuples for prefix LIKE

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kv_store ("
                "ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY (ns, key))"
            )

    def _ns_text(self, namespace: tuple[str, ...]) -> str:
        return self._SEP.join(namespace) + self._SEP

    def _ns_from_text(self, ns_text: str) -> tuple[str, ...]:
        body = ns_text[:-1]  # strip trailing separator
        return tuple(body.split(self._SEP)) if body else ()

    def batch(self, ops):  # type: ignore[override]
        from langgraph.store.base import (
            GetOp,
            Item,
            ListNamespacesOp,
            PutOp,
            SearchItem,
            SearchOp,
        )

        results: list[Any] = []
        with self._lock:
            for op in ops:
                if isinstance(op, GetOp):
                    row = self._conn.execute(
                        "SELECT value, created_at, updated_at FROM kv_store "
                        "WHERE ns=? AND key=?",
                        (self._ns_text(op.namespace), op.key),
                    ).fetchone()
                    if row is None:
                        results.append(None)
                    else:
                        results.append(
                            Item(
                                value=json.loads(row[0]),
                                key=op.key,
                                namespace=tuple(op.namespace),
                                created_at=datetime.fromisoformat(row[1]),
                                updated_at=datetime.fromisoformat(row[2]),
                            )
                        )
                elif isinstance(op, PutOp):
                    if op.value is None:
                        self._conn.execute(
                            "DELETE FROM kv_store WHERE ns=? AND key=?",
                            (self._ns_text(op.namespace), op.key),
                        )
                    else:
                        now = datetime.now(timezone.utc).isoformat()
                        self._conn.execute(
                            "INSERT INTO kv_store (ns,key,value,created_at,updated_at) "
                            "VALUES (?,?,?,?,?) ON CONFLICT(ns,key) DO UPDATE SET "
                            "value=excluded.value, updated_at=excluded.updated_at",
                            (
                                self._ns_text(op.namespace),
                                op.key,
                                json.dumps(op.value),
                                now,
                                now,
                            ),
                        )
                    results.append(None)
                elif isinstance(op, SearchOp):
                    prefix = (
                        self._ns_text(op.namespace_prefix)
                        if op.namespace_prefix
                        else ""
                    )
                    rows = self._conn.execute(
                        "SELECT ns,key,value,created_at,updated_at FROM kv_store "
                        "WHERE ns LIKE ? ORDER BY updated_at DESC",
                        (prefix + "%",),
                    ).fetchall()
                    items = []
                    for ns_text, key, value, created, updated in rows:
                        val = json.loads(value)
                        if op.filter and not all(
                            val.get(k) == v for k, v in op.filter.items()
                        ):
                            continue
                        items.append(
                            SearchItem(
                                namespace=self._ns_from_text(ns_text),
                                key=key,
                                value=val,
                                created_at=datetime.fromisoformat(created),
                                updated_at=datetime.fromisoformat(updated),
                            )
                        )
                    off = op.offset or 0
                    lim = 10 if op.limit is None else op.limit
                    results.append(items[off : off + lim])
                elif isinstance(op, ListNamespacesOp):
                    rows = self._conn.execute(
                        "SELECT DISTINCT ns FROM kv_store"
                    ).fetchall()
                    seen: set[tuple[str, ...]] = set()
                    uniq: list[tuple[str, ...]] = []
                    for (ns_text,) in rows:
                        ns = self._ns_from_text(ns_text)
                        if op.max_depth is not None:
                            ns = ns[: op.max_depth]
                        if ns not in seen:
                            seen.add(ns)
                            uniq.append(ns)
                    off = op.offset or 0
                    lim = 100 if op.limit is None else op.limit
                    results.append(uniq[off : off + lim])
                else:
                    results.append(None)
        return results

    async def abatch(self, ops):  # type: ignore[override]
        return await asyncio.to_thread(self.batch, list(ops))


# ═══════════════════════════════════════════════════════════════════════════
# Dual-mode wrapper — one sync store, usable from sync AND async code
# ═══════════════════════════════════════════════════════════════════════════


class DualModeStore:
    """A LangGraph store usable from both sync and async contexts.

    Wraps a single sync ``BaseStore`` (``SqliteStore`` on disk, or
    ``InMemoryStore`` as a fallback). Sync methods call it directly; async
    methods run it in a worker thread via ``asyncio.to_thread``. A reentrant
    lock serializes every access so the underlying sqlite connection is never
    used by two threads at once.
    """

    def __init__(self, store: "BaseStore") -> None:
        self._store = store
        # Serializes all DB access (sync calls + async calls dispatched to
        # executor threads) so the sqlite connection is touched by one thread
        # at a time.
        self._lock = threading.RLock()

    # ── Sync methods ──────────────────────────────────────────────────────
    # Each forwards **kwargs to the wrapped store so the FULL BaseStore
    # signature is supported (search: query/filter/limit/offset/refresh_ttl;
    # put: index/ttl; get: refresh_ttl) — deepagents/LangGraph call these with
    # those keywords, e.g. search(..., query=...).

    def put(self, namespace: tuple[str, ...], key: str, value: dict, /, **kwargs: Any) -> None:
        with self._lock:
            self._store.put(namespace, key, value, **kwargs)

    def get(self, namespace: tuple[str, ...], key: str, /, **kwargs: Any) -> "Item | None":
        with self._lock:
            return self._store.get(namespace, key, **kwargs)

    def search(self, namespace_prefix: tuple[str, ...], /, **kwargs: Any) -> "list[Item]":
        with self._lock:
            return self._store.search(namespace_prefix, **kwargs)

    def delete(self, namespace: tuple[str, ...], key: str, /, **kwargs: Any) -> None:
        with self._lock:
            self._store.delete(namespace, key, **kwargs)

    def list_namespaces(self, **kwargs: Any) -> "list[tuple[str, ...]]":
        with self._lock:
            return self._store.list_namespaces(**kwargs)

    # ── Async methods (run the sync store in a thread; lock serializes) ─────

    async def aput(
        self, namespace: tuple[str, ...], key: str, value: dict, /, **kwargs: Any
    ) -> None:
        await asyncio.to_thread(self.put, namespace, key, value, **kwargs)

    async def aget(
        self, namespace: tuple[str, ...], key: str, /, **kwargs: Any
    ) -> "Item | None":
        return await asyncio.to_thread(self.get, namespace, key, **kwargs)

    async def asearch(
        self, namespace_prefix: tuple[str, ...], /, **kwargs: Any
    ) -> "list[Item]":
        return await asyncio.to_thread(self.search, namespace_prefix, **kwargs)

    async def adelete(self, namespace: tuple[str, ...], key: str, /, **kwargs: Any) -> None:
        await asyncio.to_thread(self.delete, namespace, key, **kwargs)

    async def alist_namespaces(self, **kwargs: Any) -> "list[tuple[str, ...]]":
        return await asyncio.to_thread(self.list_namespaces, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# Exposed helpers — both return the same DualModeStore singleton
# ═══════════════════════════════════════════════════════════════════════════

_store: "DualModeStore | None" = None

# Path to the shared SQLite database.
_STORE_DB_PATH = HOME_DIR / "store.db"


def _build_store() -> "DualModeStore":
    """Build the durable store, with graceful degradation.

    Order of preference (all share ``~/.nova/store.db``):
      1. ``SqliteStore`` from ``langgraph-checkpoint-sqlite`` (battle-tested),
      2. the stdlib-only :class:`_StdlibSqliteStore` (durable, no extra package),
      3. ``InMemoryStore`` (ephemeral — only if even sqlite3 is unavailable).
    """
    try:
        import sqlite3

        _STORE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → autocommit; check_same_thread=False so the
        # connection can be used from executor threads (serialized by the locks).
        conn = sqlite3.connect(
            str(_STORE_DB_PATH), check_same_thread=False, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        try:
            from langgraph.store.sqlite import SqliteStore

            store: BaseStore = SqliteStore(conn)
            store.setup()
        except ModuleNotFoundError:
            # Optional langgraph-checkpoint-sqlite isn't installed — use the
            # built-in stdlib store so memory still persists across restarts.
            store = _StdlibSqliteStore(conn)
            boot_status("memory: using built-in sqlite store (durable)")
        return DualModeStore(store)
    except Exception as exc:  # noqa: BLE001
        from langgraph.store.memory import InMemoryStore

        boot_status(
            f"memory: durable store unavailable ({exc}) — using in-memory",
            "warn",
        )
        logger.warning("Durable store init failed", exc_info=True)
        return DualModeStore(InMemoryStore())


def get_durable_store() -> "DualModeStore":
    """Return the shared, durable store (sync + async), building it on first call.

    Safe to call from sync or async contexts. Falls back to an in-memory store
    if SQLite-backed storage can't be initialised, so a packaging/permissions
    problem degrades memory to ephemeral rather than crashing startup.
    """
    global _store
    if _store is None:
        _store = _build_store()
    return _store


async def get_async_durable_store() -> "DualModeStore":
    """Async-friendly accessor for the shared store.

    The store needs no async setup (async calls delegate to the sync store in a
    thread), so this just returns the singleton. Kept async for call-site
    compatibility (e.g. ``store = await get_async_durable_store()``).
    """
    return get_durable_store()


__all__ = [
    "get_durable_store",
    "get_async_durable_store",
    "DualModeStore",
]
