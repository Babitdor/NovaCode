"""Cache safety and unchanged model-boundary data acceptance checks."""

# Framework fixtures and deliberately tiny fake model/agent signatures.
# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG001, ARG002, ARG005
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli import computation_cache as cache


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def test_lru_limits_defensive_copies_and_disabled(monkeypatch):
    monkeypatch.setattr(cache, "ENTRY_LIMIT", 2)
    cache.put("a", "1", {"nested": [1]})
    cache.put("a", "2", [2])
    value = cache.get("a", "1")
    value["nested"].append(9)
    cache.put("a", "3", [3])
    assert cache.get("a", "2") is None
    assert cache.get("a", "1") == {"nested": [1]}
    monkeypatch.setenv("NOVA_DISABLE_COMPUTATION_CACHE", "1")
    assert cache.get("a", "1") is None
    cache.put("a", "4", [4])
    monkeypatch.delenv("NOVA_DISABLE_COMPUTATION_CACHE")
    assert cache.get("a", "4") is None


def test_shared_and_individual_byte_budgets(monkeypatch):
    monkeypatch.setattr(cache, "CACHE_BYTES", 20)
    monkeypatch.setattr(cache, "PROCESS_BYTES", 25)
    cache.put("a", "large", "x" * 21)
    assert cache.snapshot()["entries"] == 0
    cache.put("a", "1", "x" * 10)
    cache.put("a", "2", "x" * 10)
    assert cache.get("a", "1") is None
    cache.put("b", "1", "x" * 10)
    cache.put("c", "1", "x" * 10)
    assert cache.snapshot()["retained_bytes"] <= 25
    assert cache.get("a", "2") is None
    cache.clear("b")
    assert cache.get("b", "1") is None


def test_concurrent_cache_operations():
    def work(i):
        cache.put("a", str(i), [i])
        result = cache.get("a", str(i))
        assert result is None or result == [i]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(500)))
    assert cache.snapshot()["entries"] <= 256


def test_parsed_agent_exact_content_and_mutation(monkeypatch):
    from novacode_cli.agents.agent_file import _split

    original = "---\ntools: [one]\nskill_names: [a]\n---\nDo work"
    first = _split(original)
    first[0]["tools"].append("two")
    assert _split(original)[0]["tools"] == ["one"]
    assert _split(original.replace("one", "two"))[0]["tools"] == ["two"]
    enabled = _split(original)
    monkeypatch.setenv("NOVA_DISABLE_COMPUTATION_CACHE", "1")
    assert _split(original) == enabled


def test_named_agents_bind_current_tools_roles_and_files(monkeypatch, tmp_path):
    from langchain_core.tools import StructuredTool

    from novacode_cli.agents import core_agent as core

    path = tmp_path / "定义"
    path.mkdir()
    file = path / "agent.md"
    file.write_text("---\ntools: [one]\n---\nAAA", encoding="utf-8")
    monkeypatch.setattr(
        "novacode_cli.config.config.settings.get_all_agents",
        lambda: [("custom", path, "project")] if file.exists() else [],
    )
    role = [object()]
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_dynamic_role_model", lambda: role[0]
    )

    def one() -> str:
        """Return a value."""
        return "one"

    tool1 = StructuredTool.from_function(one)
    first = core.build_named_subagents("main", [tool1])[0]
    role[0] = object()
    tool2 = StructuredTool.from_function(one)
    second = core.build_named_subagents("main", [tool2])[0]
    assert second["tools"] == [tool2]
    assert second["tools"][0] is tool2
    assert second["model"] is role[0]
    assert second["model"] is not first["model"]
    stat = file.stat()
    file.write_text("---\ntools: [one]\n---\nBBB", encoding="utf-8")
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert core.build_named_subagents("main", [])[0]["system_prompt"] == "BBB"
    assert core.build_named_subagents("main", [])[0]["tools"] == []
    file.unlink()
    assert core.build_named_subagents("main", []) == []


def test_skill_listing_validates_restored_timestamps_and_current_sources(tmp_path, monkeypatch):
    from novacode_cli.skills.load import _list_dir
    from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware

    skill = tmp_path / "技能"
    skill.mkdir()
    file = skill / "SKILL.md"
    file.write_text("---\nname: test\ndescription: AAA\n---\nBody", encoding="utf-8")
    initial = _list_dir(tmp_path)
    assert initial[0]["description"] == "AAA"
    stat = file.stat()
    file.write_text(file.read_text(encoding="utf-8").replace("AAA", "BBB"), encoding="utf-8")
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _list_dir(tmp_path)[0]["description"] == "BBB"
    middleware = object.__new__(RefreshingSkillsMiddleware)
    middleware._watch_dirs = [tmp_path]
    middleware._library_names = None
    before = middleware._compute_signature()
    file.write_text(file.read_text(encoding="utf-8").replace("BBB", "CCC"), encoding="utf-8")
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert middleware._compute_signature() != before
    renamed = tmp_path / "renamed"
    skill.rename(renamed)
    assert "renamed" in _list_dir(tmp_path)[0]["path"]
    (renamed / "SKILL.md").unlink()
    assert _list_dir(tmp_path) == []
    assert _list_dir(tmp_path / "absent") == []


def test_persistent_tokens_reject_legacy_invalid_and_write_hashes_only(tmp_path):
    from novacode_cli.token_utils import _load_token_cache, _save_token_cache

    file = tmp_path / "token_cache.json"
    for data in [
        {"hash": "key", "tokens": 99},
        {"version": 2, "hash": "key", "tokens": True},
        {"version": 2, "hash": "key", "tokens": -1},
    ]:
        file.write_text(json.dumps(data), encoding="utf-8")
        assert _load_token_cache(tmp_path, "key") is None
    _save_token_cache(tmp_path, "key", 10)
    assert _load_token_cache(tmp_path, "key") == 10
    assert _load_token_cache(tmp_path, "other") is None
    assert set(json.loads(file.read_text())) == {"version", "hash", "tokens"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_counting_identity_model_tokenizer_versions_and_custom(monkeypatch):
    from langchain_openai import ChatOpenAI

    from novacode_cli import token_utils as tokens

    model = ChatOpenAI(model="gpt-4o", api_key="placeholder")
    initial = tokens._counting_identity(model)
    assert initial
    model.model_name = "gpt-4o-mini"
    assert tokens._counting_identity(model) != initial
    model.model_name = "gpt-4o"
    model.tiktoken_model_name = "gpt-4"
    assert tokens._counting_identity(model) != initial
    model.tiktoken_model_name = None
    monkeypatch.setattr(tokens.metadata, "version", lambda name: "changed")
    assert tokens._counting_identity(model) != initial
    model.custom_get_token_ids = lambda text: [1]
    assert tokens._counting_identity(model) is None
    assert tokens._counting_identity(object()) is None


@pytest.mark.asyncio
async def test_superseded_compaction_preserves_history(monkeypatch, tmp_path):
    from novacode_cli import compaction as c

    messages = [HumanMessage(content="constraints", id="h"), AIMessage(content="edits", id="a")]
    updates = []

    class Agent:
        async def aget_state(self, config):
            return type("State", (), {"values": {"messages": messages}})()

        async def aupdate_state(self, **kwargs):
            updates.append(kwargs)

    async def summary(*args, **kwargs):
        messages.append(HumanMessage(content="new constraint", id="h2"))
        return "old summary"

    monkeypatch.setattr(c, "summarize_conversation", summary)
    monkeypatch.setattr(c, "_archive_messages", lambda *args: None)
    result = await c.compact_conversation(Agent(), object(), "task", agent_dir=tmp_path)
    assert not result.success
    assert "changed" in result.error
    assert not updates
    assert messages[-1].content == "new constraint"
    assert not (tmp_path / "memories").exists()


@pytest.mark.asyncio
async def test_cancelled_compaction_preserves_history(monkeypatch):
    from novacode_cli import compaction as c

    messages = [HumanMessage(content="constraints", id="h")]

    class Agent:
        async def aget_state(self, config):
            return type("State", (), {"values": {"messages": messages}})()

        async def aupdate_state(self, **kwargs):
            pytest.fail("cancelled summary must not rewrite history")

    async def summary(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(c, "summarize_conversation", summary)
    monkeypatch.setattr(c, "_archive_messages", lambda *args: None)
    with pytest.raises(asyncio.CancelledError):
        await c.compact_conversation(Agent(), object(), "task")
    assert messages[0].content == "constraints"


def test_baseline_counts_bind_identity_and_never_persist_fallback(monkeypatch, tmp_path):
    from novacode_cli import token_utils as tokens

    monkeypatch.setattr("novacode_cli.config.config._find_project_root", lambda: None)
    monkeypatch.setattr(tokens, "get_memory_system_prompt", lambda *args: "standing instructions")
    monkeypatch.setattr(tokens, "_counting_identity", lambda model: model.identity)

    class Model:
        identity = "model-1"
        calls = 0
        fail = False

        def get_num_tokens_from_messages(self, messages):
            self.calls += 1
            if self.fail:
                error = "native counter unavailable"
                raise RuntimeError(error)
            return 50

    model = Model()

    def count():
        return tokens.calculate_baseline_tokens(model, tmp_path, "prompt", "agent")

    assert count() == count() == 50
    assert model.calls == 1
    model.identity = "model-2"
    assert count() == 50
    assert model.calls == 2
    (tmp_path / "agent.md").write_text("new instructions", encoding="utf-8")
    assert count() == 50
    assert model.calls == 3
    before = (tmp_path / "token_cache.json").read_bytes()
    model.identity = "model-3"
    model.fail = True
    monkeypatch.setattr(tokens, "_count_fallback_tokens", lambda *args: 12)
    assert count() == 12
    assert model.calls == 4
    assert (tmp_path / "token_cache.json").read_bytes() == before


def test_model_boundary_specs_and_schemas_match_with_cache_disabled(monkeypatch, tmp_path):
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from novacode_cli.agents import core_agent as core

    file = tmp_path / "agent.md"
    file.write_text("---\ntools: [read]\n---\nKeep user constraints", encoding="utf-8")
    monkeypatch.setattr(
        "novacode_cli.config.config.settings.get_all_agents",
        lambda: [("custom", tmp_path, "global")],
    )
    monkeypatch.setattr("novacode_cli.config.model_create.build_dynamic_role_model", lambda: None)

    def read(path: str) -> str:
        """Read a current file."""
        return path

    tool = StructuredTool.from_function(read)

    def boundary():
        spec = core.build_named_subagents("main", [tool])[0]
        return {**spec, "tools": [convert_to_openai_tool(t) for t in spec["tools"]]}

    expected = boundary()
    assert boundary() == expected
    monkeypatch.setenv("NOVA_DISABLE_COMPUTATION_CACHE", "1")
    assert boundary() == expected


def test_failed_skill_reads_never_reuse_stale_listing(monkeypatch, tmp_path):
    from pathlib import Path

    from novacode_cli.skills.load import _list_dir

    skill = tmp_path / "skill"
    skill.mkdir()
    file = skill / "SKILL.md"
    file.write_text("---\nname: test\ndescription: present\n---\nBody", encoding="utf-8")
    assert _list_dir(tmp_path)
    original = Path.read_bytes

    def read(path):
        if path == file:
            error = "read failed"
            raise OSError(error)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    # Backend reads may succeed independently; they must be fresh, not cache hits.
    hits = cache.snapshot()["caches"]["skill_definitions"].get("hit", 0)
    _list_dir(tmp_path)
    assert cache.snapshot()["caches"]["skill_definitions"].get("hit", 0) == hits


def test_dispatch_diagnostics_are_content_free_and_cost_unknown(monkeypatch, capsys):
    from novacode_cli.tracking.request_metrics import dispatch, request_purpose

    monkeypatch.setenv("NOVA_LOCAL_METRICS", "1")
    with dispatch("summary.questions", task_id="opaque-task"):
        assert request_purpose.get() == "summary.questions"
    record = json.loads(capsys.readouterr().err)["nova_local_metrics"]
    assert record["purpose"] == "summary.questions"
    assert record["task_id"] == "opaque-task"
    assert record["estimated_cost_usd"] is None
    assert record["http_attempts"] is None
    assert request_purpose.get() == "unknown"
    assert "prompt" not in record


def test_internal_section_experiment_has_provenance_and_is_expandable(tmp_path):
    from scripts.benchmark_phase45 import section_experiment

    file = tmp_path / "文件.py"
    file.write_text("one\ntwo\nthree\n", encoding="utf-8")
    excerpt = section_experiment(file, 2, 3)
    assert excerpt["path"] == str(file)
    assert excerpt["start_line"] == 2
    assert excerpt["end_line"] == 3
    assert excerpt["text"] == "two\nthree"
    assert excerpt["expandable"]
    assert excerpt["full_read_available"]


def test_discovery_picks_up_new_roots_and_workspace_switches(tmp_path):
    from novacode_cli.config.config import find_project_skills

    assert find_project_skills(tmp_path) == []
    root = tmp_path / ".agents" / "skills"
    root.mkdir(parents=True)
    assert find_project_skills(tmp_path) == [root]
    other = tmp_path / "other"
    other.mkdir()
    assert find_project_skills(other) == []
    root.rename(tmp_path / ".agents" / "renamed")
    assert find_project_skills(tmp_path) == []
