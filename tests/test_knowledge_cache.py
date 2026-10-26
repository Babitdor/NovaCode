"""Knowledge summaries are disposable context with authoritative source checks."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from novacode_cli.context.knowledge_cache import KnowledgeCache
from novacode_cli.tools.knowledge_tools import KnowledgeLookup

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def lookup(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "code.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    from deepagents.backends.filesystem import FilesystemBackend

    model = AsyncMock()
    model.ainvoke.return_value = AIMessage(content="The hello function returns world.")
    cache = KnowledgeCache(tmp_path / "cache")
    return KnowledgeLookup(
        root,
        "nova",
        FilesystemBackend(root_dir=root, virtual_mode=True),
        model,
        cache=cache,
        embed=lambda _text: None,
        model_id="test-model",
    )


async def test_cold_warm_restart_and_exact_without_embeddings(lookup: KnowledgeLookup):
    cold = await lookup.lookup("explain", ["code.py"])
    assert cold["kind"] == "generated"
    assert (await lookup.lookup("different wording", ["code.py"]))["kind"] == "exact_hit"
    lookup.cache = KnowledgeCache(lookup.cache.directory)
    assert (await lookup.lookup("overview", ["code.py"]))["kind"] == "exact_hit"
    assert lookup.model.ainvoke.await_count == 1
    assert (await lookup.lookup("explain"))["kind"] == "miss"


async def test_hash_validation_ignores_timestamps_and_deletion(lookup: KnowledgeLookup):
    await lookup.lookup("explain", ["code.py"])
    source = lookup.root() / "code.py"
    before = source.stat()
    source.write_text("def hello():\n    return 'other'\n", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "generated"
    source.unlink()
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "miss"


@pytest.mark.parametrize(
    "path",
    [
        "../code.py",
        "/code.py",
        "C:/code.py",
        ".env",
        ".nova/state.json",
        ".git/config",
        "id_rsa",
        "key.pem",
        "credentials.json",
    ],
)
async def test_private_or_escaping_sources_rejected(lookup: KnowledgeLookup, path: str):
    if not path.startswith(("/", "..", "C:")):
        source = lookup.root() / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("private contents", encoding="utf-8")
    lookup.backend.adownload_files = AsyncMock(wraps=lookup.backend.adownload_files)
    assert (await lookup.lookup("explain", [path]))["kind"] == "miss"
    lookup.model.ainvoke.assert_not_awaited()
    lookup.backend.adownload_files.assert_not_awaited()


@pytest.mark.parametrize("action", ["clear", "off"])
async def test_clear_and_off_fence_inflight_generation(lookup: KnowledgeLookup, action: str):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args: object, **_kwargs: object) -> AIMessage:
        entered.set()
        await release.wait()
        return AIMessage(content="overview")

    lookup.model.ainvoke.side_effect = generate
    pending = asyncio.create_task(lookup.lookup("explain", ["code.py"]))
    await entered.wait()
    await lookup.control(action)
    release.set()
    assert (await pending)["kind"] == "miss"
    assert (await lookup.control("status"))["retained_bytes"] == 0
    if action == "off":
        assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "disabled"


async def test_source_changed_during_generation_never_published(lookup: KnowledgeLookup):
    async def generate(*_args: object, **_kwargs: object) -> AIMessage:
        (lookup.root() / "code.py").write_text("changed", encoding="utf-8")
        return AIMessage(content="old source overview")

    lookup.model.ainvoke.side_effect = generate
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "miss"
    assert (await lookup.control("status"))["retained_bytes"] == 0


async def test_paraphrase_retrieval_and_backend_authority(lookup: KnowledgeLookup):
    lookup.embed = lambda _text: [1.0, 0.0]
    await lookup.lookup("explain", ["code.py"])
    assert (await lookup.lookup("what does this module do?"))["kind"] == "semantic_hit"
    lookup.backend.adownload_files = AsyncMock(side_effect=PermissionError("revoked"))
    assert (await lookup.lookup("module"))["kind"] == "miss"
    assert (await lookup.control("status"))["invalidations"] == 1
    assert lookup.model.ainvoke.await_count == 1


async def test_model_prompt_and_assistant_isolation(
    lookup: KnowledgeLookup, monkeypatch: pytest.MonkeyPatch
):
    await lookup.lookup("explain", ["code.py"])
    lookup.assistant = "private-agent"
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "generated"
    lookup.assistant = "nova"
    lookup.model_id = "changed-provider-config"
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "generated"
    monkeypatch.setattr("novacode_cli.tools.knowledge_tools.PROMPT_VERSION", "v2")
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "generated"
    assert lookup.model.ainvoke.await_count == 4


async def test_concurrent_producers_share_one_generation(lookup: KnowledgeLookup):
    async def generate(*_args: object, **_kwargs: object) -> AIMessage:
        await asyncio.sleep(0.1)
        return AIMessage(content="general overview")

    lookup.model.ainvoke.side_effect = generate
    results = await asyncio.gather(*(lookup.lookup(str(i), ["code.py"]) for i in range(4)))
    assert sorted(r["kind"] for r in results) == ["exact_hit"] * 3 + ["generated"]
    assert lookup.model.ainvoke.await_count == 1


async def test_cancellation_releases_producer(lookup: KnowledgeLookup):
    entered = asyncio.Event()

    async def generate(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    lookup.model.ainvoke.side_effect = generate
    task = asyncio.create_task(lookup.lookup("explain", ["code.py"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lookup.model.ainvoke.side_effect = None
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "generated"


@pytest.mark.parametrize(
    "response",
    [
        AIMessage(content=""),
        AIMessage(content="a" * 4001),
        AIMessage(content="summary", tool_calls=[{"name": "read_file", "args": {}, "id": "x"}]),
    ],
)
async def test_invalid_model_response_not_published(lookup: KnowledgeLookup, response: AIMessage):
    lookup.model.ainvoke.return_value = response
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "miss"
    assert (await lookup.control("status"))["retained_bytes"] == 0


async def test_limits_and_corruption_fall_back(lookup: KnowledgeLookup):
    source = lookup.root() / "code.py"
    for data in ("x" * 32769, "binary\0content"):
        source.write_text(data, encoding="utf-8")
        assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "miss"
    lookup.model.ainvoke.assert_not_awaited()
    await lookup.control("status")

    def corrupt() -> None:
        (lookup.cache.directory / "knowledge.sqlite3").write_bytes(b"not sqlite")

    await asyncio.to_thread(corrupt)
    assert (await lookup.lookup("query"))["kind"] == "miss"


async def test_workspace_isolation_and_scoped_switch(lookup: KnowledgeLookup, tmp_path: Path):
    await lookup.lookup("explain", ["code.py"])
    await lookup.control("off")
    other = tmp_path / "other"
    other.mkdir()
    lookup.root = lambda: other
    assert (await lookup.control("status"))["semantic_cache_enabled"] is True
    lookup.root = lambda: tmp_path / "workspace"
    assert (await lookup.control("status"))["semantic_cache_enabled"] is False
    await lookup.control("on")
    assert (await lookup.lookup("explain", ["code.py"]))["kind"] == "generated"


async def test_store_expiry_lru_and_stale_leases(
    lookup: KnowledgeLookup, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.context import knowledge_cache as store

    ns = "test"
    cache = lookup.cache
    assert await lookup._db("acquire", ns, "one", 0, "old")
    assert not await lookup._db("acquire", ns, "one", 0, "new")

    def expire() -> None:
        with cache.connection() as db:
            db.execute("UPDATE leases SET until=0")

    await asyncio.to_thread(expire)
    assert await lookup._db("acquire", ns, "one", 0, "new")
    assert not await lookup._db("publish", ns, "one", 0, "old", "[]", "old", "m", "e", None)
    assert await lookup._db("publish", ns, "one", 0, "new", "[]", "summary", "m", "e", None)
    monkeypatch.setattr(store, "MAX_ARTIFACTS", 1)
    assert await lookup._db("acquire", ns, "two", 0, "new")
    assert await lookup._db("publish", ns, "two", 0, "new", "[]", "new", "m", "e", None)
    assert [r["key"] for r in await lookup._db("candidates", ns, "m", None)] == ["two"]
    monkeypatch.setattr(store, "MAX_PAYLOAD", 1)
    assert (await lookup._db("control", ns, "status"))["retained_bytes"] == 0


async def test_cold_usage_attribution_and_zero_warm_calls(lookup: KnowledgeLookup):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    from novacode_cli.tracking import usage_tree as usage

    response = AIMessage(
        content="hello returns world",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
    )
    lookup.model = FakeMessagesListChatModel(responses=[response])
    tree = usage.UsageTree("knowledge-test")
    previous = usage.get_current_tree()
    usage.set_current_tree(tree)
    try:
        assert (await lookup.lookup("first question", ["code.py"]))["kind"] == "generated"
        assert (await lookup.lookup("second question", ["code.py"]))["kind"] == "exact_hit"
    finally:
        usage.set_current_tree(previous)
    node = tree.node_for(("knowledge_summary",))
    assert node.calls == 1
    assert node.input_tokens == 100
    assert node.output_tokens == 20
    stats = await lookup.control("status")
    assert stats["generation_calls"] == stats["exact_hit"] == 1


async def test_semantic_top_three_and_threshold(lookup: KnowledgeLookup):
    lookup.embed = lambda text: [0.0, 1.0] if "irrelevant" in text else [1.0, 0.0]
    for index in range(5):
        name = f"module{index}.py"
        (lookup.root() / name).write_text(f"value = {index}", encoding="utf-8")
        lookup.model.ainvoke.return_value = AIMessage(content=f"module {index} overview")
        await lookup.lookup("create overview", [name])
    assert len((await lookup.lookup("paraphrased question"))["artifacts"]) == 3
    assert (await lookup.lookup("irrelevant question"))["kind"] == "miss"
    assert lookup.model.ainvoke.await_count == 5


async def test_complete_hash_and_expiry(lookup: KnowledgeLookup):
    source = lookup.root() / "code.py"
    source.write_text("x\n" * 3000, encoding="utf-8")
    await lookup.lookup("overview", ["code.py"])
    source.write_text("x\n" * 2999 + "y\n", encoding="utf-8")
    assert (await lookup.lookup("overview", ["code.py"]))["kind"] == "generated"

    def expire() -> None:
        with lookup.cache.connection() as db:
            db.execute("UPDATE artifacts SET expires=0")

    await asyncio.to_thread(expire)
    assert (await lookup.lookup("overview", ["code.py"]))["kind"] == "generated"
    assert lookup.model.ainvoke.await_count == 3


async def test_symlink_escape_and_private_alias(lookup: KnowledgeLookup, tmp_path: Path):
    outside = tmp_path / "outside.py"
    outside.write_text("private outside", encoding="utf-8")
    private = lookup.root() / ".env"
    private.write_text("private credentials", encoding="utf-8")
    for index, target in enumerate((outside, private)):
        alias = lookup.root() / f"alias{index}.py"
        try:
            alias.symlink_to(target)
        except OSError:
            pytest.skip("Symlink creation unavailable on this Windows account")
        assert (await lookup.lookup("explain", [alias.name]))["kind"] == "miss"
    lookup.model.ainvoke.assert_not_awaited()


@pytest.mark.parametrize("_repeat", range(5))
async def test_independent_store_instances_share_lease(lookup: KnowledgeLookup, _repeat: int):
    from copy import copy

    other = copy(lookup)
    other.cache = KnowledgeCache(lookup.cache.directory)

    async def generate(*_args: object, **_kwargs: object) -> AIMessage:
        await asyncio.sleep(0.1)
        return AIMessage(content="shared overview")

    lookup.model.ainvoke.side_effect = generate
    errors = []
    original_db = other._db
    first_db = lookup._db

    async def db(method: str, *args: object) -> object:
        try:
            return await original_db(method, *args)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    other._db = db

    async def first(method: str, *args: object) -> object:
        try:
            return await first_db(method, *args)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    lookup._db = first
    results = await asyncio.gather(
        lookup.lookup("one", ["code.py"]), other.lookup("two", ["code.py"])
    )
    assert {row["kind"] for row in results} == {"generated", "exact_hit"}, errors
    assert lookup.model.ainvoke.await_count == 1


async def test_local_embedding_loader_never_falls_back_to_repo_id(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.skills import retrieval

    retrieval.local_model.cache_clear()
    retrieval.local_snapshot.cache_clear()
    try:
        with monkeypatch.context() as patch:
            from unittest.mock import Mock

            download = Mock(side_effect=OSError("no local weights"))
            patch.setattr("huggingface_hub.snapshot_download", download)
            assert await asyncio.to_thread(retrieval.local_model) is None
            download.assert_called_once_with(retrieval.MODEL_NAME, local_files_only=True)
    finally:
        retrieval.local_model.cache_clear()
        retrieval.local_snapshot.cache_clear()


@pytest.mark.parametrize("restricted", [False, True])
def test_core_tool_stays_out_of_delegated_rosters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, restricted: bool
):
    from unittest.mock import Mock

    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents import core_agent
    from novacode_cli.agents.tool_search import CORE_TOOLS
    from novacode_cli.mcp.config import MCPConfig

    roster_tools = []

    def roster(**kwargs: object) -> list:
        roster_tools.extend(tool.name for tool in kwargs["tools"])
        return []

    monkeypatch.setattr(MCPConfig, "load", lambda _self: {})
    monkeypatch.setattr(core_agent.settings, "project_root", tmp_path)
    monkeypatch.setattr(
        core_agent, "_build_skill_sources", lambda: ([], tmp_path, tmp_path, [], [])
    )
    monkeypatch.setattr(core_agent, "_build_middleware_stack", lambda **_kw: [])
    monkeypatch.setattr(core_agent, "_build_subagent_roster", roster)
    monkeypatch.setattr("novacode_cli.plugins.loader.discover_enabled_plugins", list)
    monkeypatch.setattr("novacode_cli.agents.invocation.definition_signature", lambda: "test")
    build = Mock(wraps=core_agent.create_deep_agent)
    monkeypatch.setattr(core_agent, "create_deep_agent", build)
    core_agent.create_agent_with_config(
        FakeListChatModel(responses=["unused"]),
        "knowledge-test",
        [],
        system_prompt="Offline",
        workspace_root=str(tmp_path),
        extra_middleware=[AgentMiddleware()] if restricted else None,
    )
    tools = {tool.name for tool in build.call_args.kwargs["tools"]}
    assert ("knowledge_lookup" in tools) is not restricted
    assert "knowledge_lookup" not in roster_tools
    assert "knowledge_lookup" in CORE_TOOLS


async def test_cache_command_uses_current_backend_workspace(
    lookup: KnowledgeLookup, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace

    from novacode_cli.commands import CommandContext, build_command_registry
    from novacode_cli.commands.cache_handler import handle_cache_command
    from novacode_cli.config.config import settings

    monkeypatch.setattr(
        type(settings), "user_deepagents_dir", property(lambda _self: lookup.cache.directory.parent)
    )
    backend = SimpleNamespace(default=SimpleNamespace(cwd=lookup.root()))
    state = SimpleNamespace(_backend=backend)
    ctx = CommandContext("cache", "off", None, None, state, "nova")
    # Use the real command and its normal default profile cache path.
    assert build_command_registry().get("cache") is handle_cache_command
    await handle_cache_command(ctx)
    other = KnowledgeLookup(lookup.root(), "nova", lookup.backend, lookup.model)
    assert (await other.control("status"))["semantic_cache_enabled"] is False
    backend.default.cwd = lookup.root().parent
    ctx.cmd_args = "status"
    await handle_cache_command(ctx)
    other.root = lambda: lookup.root().parent
    assert (await other.control("status"))["semantic_cache_enabled"] is True


async def test_summary_deadline_is_recoverable(
    lookup: KnowledgeLookup, monkeypatch: pytest.MonkeyPatch
):
    original_timeout = asyncio.timeout
    deadlines = []

    def timeout(seconds: float) -> object:
        deadlines.append(seconds)
        return original_timeout(0.01)

    async def generate(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    lookup.model.ainvoke.side_effect = generate
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "timeout", timeout)
        assert (await lookup.lookup("overview", ["code.py"]))["kind"] == "miss"
    assert deadlines == [60]
    assert (await lookup.control("status"))["retained_bytes"] == 0


async def test_provider_text_blocks_exclude_reasoning(lookup: KnowledgeLookup):
    lookup.model.ainvoke.return_value = AIMessage(
        content=[
            {"type": "thinking", "thinking": "private provider reasoning"},
            {"type": "text", "text": "The hello function returns world."},
        ]
    )
    result = await lookup.lookup("explain", ["code.py"])
    assert result["kind"] == "generated"
    assert result["artifacts"][0]["summary"] == "The hello function returns world."
