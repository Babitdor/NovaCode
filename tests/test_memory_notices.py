"""Memory discovery stays selective and its actual use is visible without leaking bodies."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, ToolMessage

from tests.test_memory_prompt_cache_order import _bare_middleware

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_outgoing_model_request_does_not_auto_include_lessons(
    tmp_path: Path,
    *,
    asynchronous: bool,
) -> None:
    middleware = _bare_middleware(
        tmp_path, topics={"windows-packaging": "- Windows packaging requires DLL_SENTINEL."}
    )
    notices = []
    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]),
        messages=[HumanMessage("Windows packaging")],
        system_prompt="BASE",
        state={"user_memory": "USER_SENTINEL", "memory_index": "INDEX_SENTINEL"},
        runtime=SimpleNamespace(stream_writer=notices.append),
    )

    def capture(outgoing: ModelRequest) -> str:
        assert "USER_SENTINEL" in outgoing.system_message.content
        assert "INDEX_SENTINEL" in outgoing.system_message.content
        assert "DLL_SENTINEL" not in outgoing.system_message.content
        return "captured"

    if asynchronous:
        assert (
            await middleware.awrap_model_call(request, AsyncMock(side_effect=capture)) == "captured"
        )
    else:
        assert middleware.wrap_model_call(request, capture) == "captured"
    assert notices == []
    assert "DLL_SENTINEL" not in repr(notices)
    assert len(request.messages) == 1


async def test_scoped_topic_search_returns_its_real_path_without_announcing_a_read(
    tmp_path: Path,
) -> None:
    middleware = _bare_middleware(tmp_path, topics={"packaging": "- General packaging guidance."})
    middleware._project_key = "nova"
    folder = middleware.agent_dir / "memories/projects/nova"
    folder.mkdir(parents=True)
    (folder / "packaging.md").write_text("- Windows packaging uses uv.", encoding="utf-8")
    result = await middleware._memory_search("packaging")
    assert "/memories/memories/projects/nova/packaging.md" in result
    assert "/memories/memories/packaging.md" in result


async def test_generated_knowledge_never_invents_a_page_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli.memory.parsed_cache import MemoryCorpus, ParsedMemoryCache
    from novacode_cli.memory.search import _tokens

    middleware = _bare_middleware(tmp_path)
    corpus = MemoryCorpus(ParsedMemoryCache(), _tokens)
    corpus.add("learning-123", _tokens("packaging"), "- Windows packaging uses uv.", "digest")
    middleware._memory_search_service._corpus_cache = corpus
    monkeypatch.setattr(middleware._memory_search_service, "_load_memory_corpus", lambda: corpus)
    found = await middleware._memory_search("Windows packaging")
    assert "no backing file" in found
    assert "/memories/memories/learning-123.md" not in found


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("tool", ["read_file", "read_memory"])
async def test_successful_page_reads_emit_notices(
    tmp_path: Path, tool: str, *, asynchronous: bool
) -> None:
    middleware = _bare_middleware(tmp_path)
    notices = []
    path = "/memories/memories/windows-packaging.md"
    request = SimpleNamespace(
        tool_call={"name": tool, "args": {"file_path" if tool == "read_file" else "path": path}},
        runtime=SimpleNamespace(stream_writer=notices.append),
    )
    content = (
        "PRIVATE_BODY"
        if tool == "read_file"
        else json.dumps(
            {
                "success": True,
                "exists": True,
                "content": "PRIVATE_BODY",
                "path": path,
            }
        )
    )
    result = ToolMessage(content=content, tool_call_id="read")
    if asynchronous:
        assert await middleware.awrap_tool_call(request, AsyncMock(return_value=result)) is result
    else:
        assert middleware.wrap_tool_call(request, lambda _request: result) is result
    assert notices == [
        {
            "type": "nova_memory_read",
            "action": "read",
            "name": "windows-packaging",
            "path": path,
        }
    ]
    assert "PRIVATE_BODY" not in repr(notices)


@pytest.mark.parametrize(
    ("tool", "content", "status", "path"),
    [
        ("read_file", "denied", "error", "/memories/memories/topic.md"),
        ("read_file", "Error: not found", "success", "/memories/memories/topic.md"),
        ("read_file", "ordinary file", "success", "/src/topic.md"),
        ("read_memory", '{"success": false}', "success", "/memories/memories/topic.md"),
        (
            "read_memory",
            '{"success": true, "exists": false}',
            "success",
            "/memories/memories/topic.md",
        ),
        ("memory_search", "topic summary", "success", "/memories/memories/topic.md"),
    ],
)
async def test_failed_reads_and_discovery_do_not_announce_use(
    tmp_path: Path,
    tool: str,
    content: str,
    status: str,
    path: str,
) -> None:
    middleware = _bare_middleware(tmp_path)
    notices = []
    request = SimpleNamespace(
        tool_call={"name": tool, "args": {"file_path": path, "path": path}},
        runtime=SimpleNamespace(stream_writer=notices.append),
    )
    result = ToolMessage(content=content, status=status, tool_call_id="read")
    await middleware.awrap_tool_call(request, AsyncMock(return_value=result))
    assert not notices


@pytest.mark.parametrize("code_mode", [False, True])
@pytest.mark.timeout(40)
async def test_real_graph_streams_page_reads_in_both_tool_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    code_mode: bool,
) -> None:
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    from novacode_cli.agents.code_mode import CodeModeMiddleware
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.memory.agent_memory import AgentMemoryMiddleware
    from tests.test_mcp_code_mode import ScriptedModel
    from tests.test_project_memory import _FakeSettings

    folder = tmp_path / "agent"
    (folder / "memories").mkdir(parents=True)
    path = "/memories/memories/packaging.md"
    (folder / "memories/packaging.md").write_text(
        "- Windows packaging uses BODY_SENTINEL.",
        encoding="utf-8",
    )
    middleware = AgentMemoryMiddleware(
        settings=_FakeSettings(tmp_path, folder),
        assistant_id="audit",
    )
    monkeypatch.setattr(middleware, "_build_learning_overview", lambda: "")
    monkeypatch.setattr(NovaConfig, "get_code_mode_enabled", lambda _self: code_mode)

    @tool
    def read_file(file_path: str) -> str:
        """Read one memory page."""
        assert file_path == path
        return "FULL_PAGE_SENTINEL"

    call = {"id": "read", "name": "read_file", "args": {"file_path": path}}
    if code_mode:
        call = {
            "id": "eval",
            "name": "eval",
            "args": {
                "code": f"await tools.call('read_file', {{file_path: '{path}'}}); 'selected';",
            },
        }
    graph = create_agent(
        ScriptedModel(messages=iter([AIMessage(content="", tool_calls=[call]), AIMessage("done")])),
        tools=[read_file],
        middleware=[*([CodeModeMiddleware(auto_approve=True)] if code_mode else []), middleware],
    )
    notices, updates = [], []
    async for mode, data in graph.astream(
        {"messages": [HumanMessage("Windows packaging")]},
        stream_mode=["custom", "updates"],
    ):
        (notices if mode == "custom" else updates).append(data)
    assert {notice["action"] for notice in notices if notice.get("type") == "nova_memory_read"} == {
        "read",
    }, updates
    assert "BODY_SENTINEL" not in repr(notices)
    assert "FULL_PAGE_SENTINEL" not in repr(notices)
    if code_mode:
        messages = [
            message
            for update in updates
            for state in update.values()
            if isinstance(state, dict)
            for message in state.get("messages", [])
        ]
        assert "FULL_PAGE_SENTINEL" not in repr(messages)
