"""Generated compaction and recovery invariants, without provider calls."""

import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from novacode_cli.agents.context_compaction import pending_tool_start, user_messages
from novacode_cli.context.archives import RecoveryStore
from novacode_cli.memory.parsed_cache import ParsedMemoryCache, file_hash


_constraint = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(["a", "Z", "0", "é", "中", "🙂"]),
    st.text(alphabet=["a", "Z", "0", " ", "\n", "é", "中", "🙂", "'", '"'], max_size=200),
)


@given(st.lists(_constraint, min_size=1, max_size=20))
def test_repeated_summary_quotations_preserve_every_constraint(texts):
    history = []
    for text in texts:
        history.append(HumanMessage(text))
        preserved = user_messages(history, 0)
        assert text in preserved
        history = [
            HumanMessage(
                "summary",
                additional_kwargs={
                    "lc_source": "summarization",
                    "user_messages": preserved,
                },
            )
        ]
    preserved = user_messages(history, 0)
    assert preserved == texts


@given(st.integers(min_value=0, max_value=25), st.integers(min_value=1, max_value=5))
def test_tool_pairing_keeps_all_outstanding_calls(completed, outstanding):
    messages = [HumanMessage("task")]
    for index in range(completed):
        messages.extend(
            [
                AIMessage("", tool_calls=[{"id": str(index), "name": "work", "args": {}}]),
                ToolMessage("done", tool_call_id=str(index)),
            ]
        )
    start = len(messages)
    messages.append(
        AIMessage(
            "",
            tool_calls=[
                {"id": f"pending-{index}", "name": "work", "args": {}}
                for index in range(outstanding)
            ],
        )
    )
    for index in range(outstanding - 1):
        messages.append(ToolMessage("done", tool_call_id=f"pending-{index}"))
    assert pending_tool_start(messages) == start
    assert len(messages[start].tool_calls) == outstanding


@given(st.binary(max_size=2048), st.text(max_size=1024))
@settings(deadline=None)  # Filesystem timing is benchmarked separately from round-trip correctness.
def test_archive_roundtrip_binary_artifacts_and_unicode(binary, text):
    with tempfile.TemporaryDirectory() as directory:
        store = RecoveryStore(Path(directory))
        messages = [
            HumanMessage(text, id="user"),
            ToolMessage(
                "result",
                tool_call_id="call",
                artifact={"binary": binary},
                id="tool",
            ),
        ]
        reference = store.archive("thread", messages)
        _, payload = store.inspect(reference.stem)
        assert payload["messages"] == messages_to_dict(messages)


def test_parsed_memory_lru_and_both_limits():
    cache = ParsedMemoryCache(max_entries=2, max_bytes=4096)
    cache["a"] = "a"
    cache["b"] = "b"
    assert cache["a"] == "a"
    cache["c"] = "c"
    assert list(cache) == ["a", "c"]
    cache["huge"] = "x" * 8192
    assert "huge" not in cache
    assert len(cache) == 2
    assert cache.resident_bytes <= cache.max_bytes
    for index in range(50):
        cache[str(index)] = "x" * 1000
    assert len(cache) <= 2
    assert cache.resident_bytes <= 4096
    assert cache["49"] == "x" * 1000
    cache["49"] = "replacement"
    before = cache.resident_bytes
    del cache["49"]
    assert cache.resident_bytes < before


def test_streamed_hash_changes_even_when_timestamp_restored(tmp_path):
    import os

    path = tmp_path / "memory.md"
    path.write_text("first")
    timestamp = path.stat().st_mtime_ns
    before = file_hash(path)
    path.write_text("other")
    os.utime(path, ns=(timestamp, timestamp))
    assert file_hash(path) != before


def test_large_corpus_cache_eviction_does_not_hide_topics_or_change_order(tmp_path):
    from novacode_cli.memory.agent_memory import AgentMemoryMiddleware

    root = tmp_path / "memories"
    root.mkdir()
    for index in range(600):
        (root / f"topic-{index:03}.md").write_text(f"distinct migration evidence {index}")
    middleware = AgentMemoryMiddleware.__new__(AgentMemoryMiddleware)
    middleware.agent_dir = tmp_path
    middleware._corpus_cache = None
    middleware._corpus_sig = None
    corpus = middleware._load_memory_corpus()
    expected = [path.stem for path in root.glob("*.md")]
    assert list(corpus)[:600] == expected
    for key in expected:
        assert corpus[key][1] == (root / (key + ".md")).read_text()
    cache = middleware._parsed_memory_cache
    assert len(cache) == 512
    assert cache.resident_bytes <= 16 * 1024 * 1024
    assert "evidence 0" in corpus["topic-000"][1]
    assert list(corpus)[:600] == expected
    assert list(middleware._load_memory_corpus())[:600] == expected
