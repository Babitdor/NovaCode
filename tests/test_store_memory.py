"""Tests for durable structured memory (SqliteStore + remember/recall tools).

This file is organised in two sections:
1. ``store.py`` tests — the ``get_durable_store()`` factory
2. Tool tests — ``remember`` / ``recall`` / ``list_memories`` / ``forget``
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

from novacode_cli.memory.store import get_durable_store
from novacode_cli.tools.store_memory_tools import (
    _NAMESPACE,
    forget,
    list_memories,
    recall,
    remember,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── store.py tests ──────────────────────────────────────────────────────────


def test_store_database_path_is_not_the_original_user_store(
    original_nova_home: Path,
) -> None:
    from novacode_cli.memory.store import _STORE_DB_PATH

    assert original_nova_home / "store.db" != _STORE_DB_PATH


def test_get_durable_store_returns_a_store():
    """``get_durable_store()`` returns a ``BaseStore`` instance."""
    store = get_durable_store()
    # It must quack like a store — has the core API methods.
    assert hasattr(store, "put")
    assert hasattr(store, "get")
    assert hasattr(store, "search")
    assert hasattr(store, "delete")


def test_get_durable_store_is_singleton():
    """Repeated calls return the *same* object (process-lifetime cache)."""
    first = get_durable_store()
    second = get_durable_store()
    assert first is second


def test_get_durable_store_can_roundtrip_a_value():
    """A value written via ``store.put`` is readable via ``store.get``."""
    store = get_durable_store()
    store.put(("ns",), "test-key", {"content": "hello from TDD"})
    item = store.get(("ns",), "test-key")
    assert item is not None
    assert item.value["content"] == "hello from TDD"


def _runtime(store):
    return SimpleNamespace(store=store)


def _call(tool, **kwargs):
    """Invoke a langchain @tool's underlying function directly."""
    return tool.func(**kwargs)


def test_sqlite_store_persists_across_reopen(tmp_path):
    """A SqliteStore-backed value survives closing and reopening the DB."""
    from langgraph.store.sqlite import SqliteStore

    db = str(tmp_path / "store.db")

    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn)
    store.setup()
    store.put(("ns",), "k", {"content": "durable!"})
    conn.close()

    # Reopen — simulating a process restart.
    conn2 = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    store2 = SqliteStore(conn2)
    item = store2.get(("ns",), "k")
    conn2.close()

    assert item is not None
    assert item.value["content"] == "durable!"


def _fresh_store():
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()


def test_remember_and_recall_roundtrip():
    store = _fresh_store()
    rt = _runtime(store)

    res = _call(
        remember,
        key="build-cmd",
        content="uv run pytest -q",
        runtime=rt,
        tags=["ci", "test"],
        category="commands",
    )
    assert res["success"] is True
    assert res["key"] == "build-cmd"

    got = _call(recall, key="build-cmd", runtime=rt)
    assert got["found"] is True
    assert got["content"] == "uv run pytest -q"
    assert got["category"] == "commands"
    assert "ci" in got["tags"]
    assert got["author"]  # attribution recorded


def test_recall_missing_key():
    rt = _runtime(_fresh_store())
    got = _call(recall, key="nope", runtime=rt)
    assert got["success"] is True
    assert got["found"] is False


def test_list_memories_filters():
    store = _fresh_store()
    rt = _runtime(store)
    _call(remember, key="a", content="deploy via fly", runtime=rt, tags=["deploy"], category="ops")
    _call(remember, key="b", content="run black on save", runtime=rt, tags=["style"], category="dev")

    # query substring
    q = _call(list_memories, runtime=rt, query="deploy")
    assert q["count"] == 1 and q["memories"][0]["key"] == "a"

    # tag filter
    t = _call(list_memories, runtime=rt, tag="style")
    assert t["count"] == 1 and t["memories"][0]["key"] == "b"

    # category filter
    c = _call(list_memories, runtime=rt, category="ops")
    assert c["count"] == 1 and c["memories"][0]["key"] == "a"

    # no filter -> both
    allm = _call(list_memories, runtime=rt)
    assert allm["count"] == 2


def test_remember_overwrites_same_key():
    store = _fresh_store()
    rt = _runtime(store)
    _call(remember, key="x", content="v1", runtime=rt)
    _call(remember, key="x", content="v2", runtime=rt)
    got = _call(recall, key="x", runtime=rt)
    assert got["content"] == "v2"


def test_forget_removes():
    store = _fresh_store()
    rt = _runtime(store)
    _call(remember, key="temp", content="bye", runtime=rt)
    _call(forget, key="temp", runtime=rt)
    got = _call(recall, key="temp", runtime=rt)
    assert got["found"] is False


def test_tools_handle_missing_store():
    rt = SimpleNamespace(store=None)
    assert _call(remember, key="k", content="c", runtime=rt)["success"] is False
    assert _call(recall, key="k", runtime=rt)["success"] is False
    assert _call(list_memories, runtime=rt)["success"] is False


def test_namespace_is_shared():
    """Sanity: the tools all use one shared namespace (cross-agent visibility)."""
    assert _NAMESPACE == ("nova_memory",)
