"""The per-turn retrieval block must not sit inside the cached prompt prefix.

Anthropic caches a *prefix* (``tools -> system -> messages``): a change at any
level invalidates that level and everything after it. Nova injects a
query-dependent ``<relevant_memory>`` block every turn, so if it precedes the
base system prompt -- or shares the block carrying the ``cache_control``
breakpoint -- every distinctly-worded turn is a cache miss and the whole base
prompt is re-billed at full input price.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import HumanMessage

from novacode_cli.memory.agent_memory import AgentMemoryMiddleware


class _Anthropicish:
    """Stands in for an Anthropic chat model (module name drives the branch)."""


_Anthropicish.__module__ = "langchain_anthropic.chat_models"


def _request(model: Any, prompt: str = "BASE SYSTEM PROMPT") -> Any:
    """A ModelRequest double. Typed Any because it stands in for the real class."""
    return SimpleNamespace(model=model, system_prompt=prompt)


def _bare_middleware(tmp_path, *, topics: dict[str, str] | None = None) -> Any:
    """A middleware instance with only the attributes the prompt path needs.

    Bypasses __init__ (which needs a full Settings) so the test exercises the
    real prompt/retrieval code without a project fixture.
    """
    agent_dir = tmp_path / "agent"
    mem_dir = agent_dir / "memories"
    mem_dir.mkdir(parents=True)
    for name, body in (topics or {}).items():
        (mem_dir / f"{name}.md").write_text(body, encoding="utf-8")

    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = agent_dir
    mw.assistant_id = "nova-agent"
    mw.skip_project_memory = True
    mw.loaded_project_memory_sources = []
    mw._backend = None
    mw.system_prompt_template = "{user_memory}\n{project_memory}"
    mw.project_root = None
    mw.agent_dir_absolute = "/memories/"
    mw.agent_dir_display = "/memories/"
    mw._memory_section_cache = None
    mw._memory_section_cache_time = 0
    mw._memory_section_cache_ttl = 30.0
    mw._cached_user_memory = None
    mw._cached_project_memory = None
    mw._cached_memory_index = None
    mw._cached_habits_memory = None
    mw._cached_learning_overview = None
    mw._corpus_cache = None
    mw._corpus_sig = None
    mw._retrieval_cache = None
    return mw


def _req(mw: Any, text: str = "hello") -> Any:
    return SimpleNamespace(
        state={}, system_prompt="BASE", messages=[HumanMessage(content=text)]
    )


# -- cache-block structure -------------------------------------------------


def test_breakpoint_lands_on_the_stable_block_only() -> None:
    msg = AgentMemoryMiddleware._make_system_message(
        _request(_Anthropicish()), "STABLE", "VOLATILE"
    )

    content: Any = msg.content
    assert isinstance(content, list)
    assert len(content) == 2

    stable, volatile = content
    assert stable["text"] == "STABLE"
    assert stable["cache_control"] == {"type": "ephemeral"}
    # The volatile block must be AFTER the breakpoint, with no cache_control.
    assert volatile["text"] == "VOLATILE"
    assert "cache_control" not in volatile


def test_volatile_block_omitted_when_nothing_retrieved() -> None:
    msg = AgentMemoryMiddleware._make_system_message(_request(_Anthropicish()), "STABLE", "")

    content: Any = msg.content
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_non_anthropic_models_get_plain_concatenation() -> None:
    class _OpenAIish:
        pass

    _OpenAIish.__module__ = "langchain_openai.chat_models"

    msg = AgentMemoryMiddleware._make_system_message(_request(_OpenAIish()), "STABLE", "VOLATILE")

    assert isinstance(msg.content, str)
    assert msg.content == "STABLE\n\nVOLATILE"


# -- the property that makes the prefix cacheable --------------------------


def test_stable_prompt_is_identical_across_differently_worded_turns(tmp_path) -> None:
    """The cacheable prefix must not move when the user rephrases.

    This is the regression: with the retrieval block ahead of the base prompt,
    unrelated wording produced a different prefix and the cache never hit.
    """
    mw = _bare_middleware(tmp_path, topics={"context-budgeting": "context window budgeting"})
    mw._cached_user_memory = "UM"
    mw._cached_project_memory = "PM"
    state = {"user_memory": "UM", "project_memory": "PM"}

    first = mw._build_system_prompt(
        SimpleNamespace(
            state=state,
            system_prompt="BASE",
            messages=[HumanMessage(content="audit the context engineering of nova")],
        )
    )
    second = mw._build_system_prompt(
        SimpleNamespace(
            state=state,
            system_prompt="BASE",
            messages=[HumanMessage(content="something entirely unrelated here")],
        )
    )

    assert first == second, "stable prompt changed with the user's wording"


def test_volatile_part_carries_the_retrieved_lessons(tmp_path) -> None:
    """Retrieval still works — it just lives in the volatile part now."""
    mw = _bare_middleware(tmp_path, topics={"context-budgeting": "context window budgeting lessons"})

    stable, volatile = mw._build_system_prompt_parts(
        _req(mw, "please audit context window budgeting")
    )

    assert "context window budgeting lessons" in volatile
    assert "<relevant_memory>" in volatile
    assert "context window budgeting lessons" not in stable
    assert "<relevant_memory>" not in stable


def test_parts_builder_returns_two_strings(tmp_path) -> None:
    mw = _bare_middleware(tmp_path)

    stable, volatile = mw._build_system_prompt_parts(_req(mw))

    assert isinstance(stable, str) and isinstance(volatile, str)
    assert "BASE" in stable


def test_latest_user_text_reads_only_human_roles() -> None:
    req = SimpleNamespace(
        messages=[
            HumanMessage(content="find the context audit"),
            SimpleNamespace(type="ai", content="ignored"),
        ]
    )
    assert AgentMemoryMiddleware._latest_user_text(cast("Any", req)) == "find the context audit"


def test_retrieval_cache_is_keyed_on_the_query(tmp_path) -> None:
    """A tool-loop iteration reusing the same query must not re-scan the corpus."""
    mw = _bare_middleware(tmp_path, topics={"context-budgeting": "context window budgeting"})
    req = _req(mw, "context budgeting")

    first = mw._relevant_memories(req)
    # Poison the corpus to prove the second call was served from cache.
    mw._corpus_cache, mw._corpus_sig = {}, None
    second = mw._relevant_memories(req)

    assert first == second


def test_missing_memories_dir_degrades_to_no_retrieval(tmp_path) -> None:
    mw = _bare_middleware(tmp_path)
    mem_dir = mw.agent_dir / "memories"
    for child in mem_dir.iterdir():
        child.unlink()
    mem_dir.rmdir()

    assert mw._relevant_memories(_req(mw, "context budgeting")) == ""


# -- corpus cache freshness (the NTFS trap) --------------------------------


def test_corpus_cache_detects_in_place_rewrite(tmp_path) -> None:
    """Rewriting a file in place must invalidate the corpus.

    On NTFS a file rewrite does not bump the *directory* mtime (only
    create/delete/rename do), and the review passes rewrite topic files in
    place — so a dir-mtime-keyed cache served stale lessons all session.
    """
    mw = _bare_middleware(tmp_path, topics={"context-budgeting": "OLD CONTENT"})
    first = mw._load_memory_corpus()
    assert "OLD CONTENT" in first["context-budgeting"][1]

    time.sleep(0.02)
    (mw.agent_dir / "memories" / "context-budgeting.md").write_text(
        "NEW CONTENT after a review pass", encoding="utf-8"
    )

    second = mw._load_memory_corpus()
    assert "NEW CONTENT" in second["context-budgeting"][1], (
        "in-place rewrite not picked up — corpus cache keyed on dir mtime"
    )


def test_corpus_cache_invalidates_on_deleting_a_non_newest_file(tmp_path) -> None:
    """Deleting a topic file must invalidate the corpus even if it wasn't newest.

    The key is ``(file_count, newest_file_mtime)``. Comparing only the newest
    mtime discarded the count, so a file deleted while a *newer* file existed
    left its lessons in the cached corpus and kept injecting them.
    """
    mw = _bare_middleware(tmp_path, topics={"aaa-old": "OLD LESSON", "zzz-new": "NEW"})
    assert "aaa-old" in mw._load_memory_corpus()
    mem_dir = mw.agent_dir / "memories"

    # Make zzz-new unambiguously newest, then re-warm against that signature.
    time.sleep(0.02)
    (mem_dir / "zzz-new.md").write_text("NEW lesson, touched", encoding="utf-8")
    mw._load_memory_corpus()
    sig_before = AgentMemoryMiddleware._corpus_signature(mem_dir)
    assert sig_before is not None
    newest_before = sig_before[1]

    (mem_dir / "aaa-old.md").unlink()

    after = AgentMemoryMiddleware._corpus_signature(mem_dir)
    assert after is not None
    # Guard the guard: if the max mtime shifted, this test would pass for the
    # wrong reason (a max-only key would have caught it too).
    assert after[1] == newest_before, "test bug: the newest mtime changed"

    assert "aaa-old" not in mw._load_memory_corpus(), (
        "a deleted topic was still served — the corpus key ignored the file count"
    )


def test_corpus_cache_reused_when_files_unchanged(tmp_path) -> None:
    mw = _bare_middleware(tmp_path, topics={"a-topic": "body"})
    assert mw._load_memory_corpus() is mw._load_memory_corpus()


def test_corpus_signature_uses_newest_file_mtime(tmp_path) -> None:
    """Signature must track file mtimes, not the directory's own mtime."""
    mw = _bare_middleware(tmp_path, topics={"a-topic": "body"})
    mem_dir = mw.agent_dir / "memories"

    before = AgentMemoryMiddleware._corpus_signature(mem_dir)
    assert before is not None
    count, newest = before
    assert count == 1
    assert newest > 0

