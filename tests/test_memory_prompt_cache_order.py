"""Learned topic bodies are searched explicitly, not injected into each prompt.

Anthropic caches a *prefix* (``tools -> system -> messages``). The stable
prompt remains cacheable, and learned topic bodies are available through the
explicit memory tools when a task needs them.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage

from novacode_cli.memory.agent_memory import AgentMemoryMiddleware
from novacode_cli.memory.search import MemorySearchService

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_anthropic import ChatAnthropic


def _anthropic_adapter() -> ChatAnthropic:
    """An offline native Anthropic adapter with explicit Nova routing metadata."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key="test-only",
        metadata={"nova_provider": "anthropic", "nova_model": "claude-sonnet-4-6"},
    )


def _request(model: Any, prompt: str = "BASE SYSTEM PROMPT") -> Any:
    """A ModelRequest double. Typed Any because it stands in for the real class."""
    return SimpleNamespace(model=model, system_prompt=prompt)


def _bare_middleware(tmp_path, *, topics: dict[str, str] | None = None) -> Any:
    """A middleware instance with only the attributes the prompt path needs.

    Bypasses __init__ (which needs a full Settings) so the test exercises the
    real prompt code without a project fixture.
    """
    agent_dir = tmp_path / "agent"
    mem_dir = agent_dir / "memories"
    mem_dir.mkdir(parents=True)
    for name, body in (topics or {}).items():
        (mem_dir / f"{name}.md").write_text(body, encoding="utf-8")

    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = agent_dir
    mw._project_key = ""
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
    mw._memory_search_service = MemorySearchService(agent_dir)
    return mw


def _req(mw: Any, text: str = "hello") -> Any:
    return SimpleNamespace(state={}, system_prompt="BASE", messages=[HumanMessage(content=text)])


# -- cache-block structure -------------------------------------------------


def test_breakpoint_lands_on_the_stable_block_only() -> None:
    msg = AgentMemoryMiddleware._make_system_message(
        _request(_anthropic_adapter()), "STABLE", "VOLATILE"
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
    msg = AgentMemoryMiddleware._make_system_message(_request(_anthropic_adapter()), "STABLE", "")

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
    """The cacheable prefix must not move when the user rephrases."""
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


def test_memory_updates_keep_the_core_prompt_prefix_cacheable(tmp_path) -> None:
    mw = _bare_middleware(tmp_path)
    base = "CORE INSTRUCTIONS " * 1000
    req = _request(_anthropic_adapter(), base)
    req.state = {"user_memory": "earlier lesson"}
    before = mw._build_system_prompt(req)
    req.state = {"user_memory": "updated lesson"}
    after = mw._build_system_prompt(req)
    assert before != after, "updated memory must remain immediately visible"
    assert before.startswith(base) and after.startswith(base)
    first = AgentMemoryMiddleware._make_system_message(req, before, "retrieved A")
    second = AgentMemoryMiddleware._make_system_message(req, after, "retrieved B")
    assert first.content[0] == second.content[0]
    assert first.content[0]["text"] == base
    assert first.content[0]["cache_control"] == {"type": "ephemeral"}
    assert first.content[1]["text"] != second.content[1]["text"]
    assert first.content[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in second.content[-1]


def test_matching_lessons_are_not_automatically_injected(tmp_path: Path) -> None:
    mw = _bare_middleware(
        tmp_path, topics={"context-budgeting": "context window budgeting lessons"}
    )

    stable, volatile = mw._build_system_prompt_parts(
        _req(mw, "please audit context window budgeting")
    )

    assert volatile == ""
    assert "context window budgeting lessons" not in stable


def test_parts_builder_returns_two_strings(tmp_path) -> None:
    mw = _bare_middleware(tmp_path)

    stable, volatile = mw._build_system_prompt_parts(_req(mw))

    assert isinstance(stable, str) and isinstance(volatile, str)
    assert "BASE" in stable


def test_missing_memories_dir_does_not_affect_prompt_building(tmp_path: Path) -> None:
    mw = _bare_middleware(tmp_path)
    mem_dir = mw.agent_dir / "memories"
    for child in mem_dir.iterdir():
        child.unlink()
    mem_dir.rmdir()

    stable, volatile = mw._build_system_prompt_parts(_req(mw, "context budgeting"))
    assert "BASE" in stable
    assert volatile == ""


# -- corpus cache freshness (the NTFS trap) --------------------------------


def test_corpus_cache_detects_in_place_rewrite(tmp_path) -> None:
    """Rewriting a file in place must invalidate the corpus.

    On NTFS a file rewrite does not bump the *directory* mtime (only
    create/delete/rename do), and the review passes rewrite topic files in
    place — so a dir-mtime-keyed cache served stale lessons all session.
    """
    mw = _bare_middleware(tmp_path, topics={"context-budgeting": "OLD CONTENT"})
    first = mw._memory_search_service._load_memory_corpus()
    assert "OLD CONTENT" in first["context-budgeting"][1]

    time.sleep(0.02)
    (mw.agent_dir / "memories" / "context-budgeting.md").write_text(
        "NEW CONTENT after a review pass", encoding="utf-8"
    )

    second = mw._memory_search_service._load_memory_corpus()
    assert "NEW CONTENT" in second["context-budgeting"][1], (
        "in-place rewrite not picked up — corpus cache keyed on dir mtime"
    )


def test_corpus_cache_invalidates_on_deleting_a_non_newest_file(tmp_path) -> None:
    """Deleting a topic file must invalidate the corpus used for search.

    Comparing only the newest mtime left a deleted topic in the cache while a
    *newer* file still existed.
    """
    mw = _bare_middleware(tmp_path, topics={"aaa-old": "OLD LESSON", "zzz-new": "NEW"})
    assert "aaa-old" in mw._memory_search_service._load_memory_corpus()
    mem_dir = mw.agent_dir / "memories"

    # Make zzz-new unambiguously newest, then re-warm against that signature.
    time.sleep(0.02)
    (mem_dir / "zzz-new.md").write_text("NEW lesson, touched", encoding="utf-8")
    mw._memory_search_service._load_memory_corpus()
    sig_before = MemorySearchService.corpus_signature(mem_dir)
    assert sig_before is not None
    newest_before = dict(sig_before)["zzz-new.md"]

    (mem_dir / "aaa-old.md").unlink()

    after = MemorySearchService.corpus_signature(mem_dir)
    assert after is not None
    # Guard the guard: if the max mtime shifted, this test would pass for the
    # wrong reason (a max-only key would have caught it too).
    assert dict(after)["zzz-new.md"] == newest_before, "test bug: surviving content changed"

    assert "aaa-old" not in mw._memory_search_service._load_memory_corpus(), (
        "a deleted topic was still served — the corpus key ignored the file count"
    )


def test_corpus_cache_reused_when_files_unchanged(tmp_path) -> None:
    mw = _bare_middleware(tmp_path, topics={"a-topic": "body"})
    assert (
        mw._memory_search_service._load_memory_corpus()
        is mw._memory_search_service._load_memory_corpus()
    )


def test_corpus_signature_uses_current_file_content(tmp_path) -> None:
    """Content identity remains valid even when timestamps are restored."""
    mw = _bare_middleware(tmp_path, topics={"a-topic": "body"})
    mem_dir = mw.agent_dir / "memories"

    before = MemorySearchService.corpus_signature(mem_dir)
    assert before is not None
    assert len(before) == 1
    assert before[0][0] == "a-topic.md"
    assert len(before[0][1]) == 64
