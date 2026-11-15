"""Discovery boundaries, verified contracts and durable selected-result recovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import CompositeBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.tools import tool
from mcp.types import Tool as MCPTool

from novacode_cli.agents.code_mode import CodeModeMiddleware, NativeInput
from novacode_cli.agents.tool_contracts import native_returns
from novacode_cli.agents.tool_offload import save_tool_result
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend
from novacode_cli.mcp.generic import build_mcp_tool
from novacode_cli.security.policy import get_policy
from tests.test_mcp_code_mode import execute, inventory, isolated_policy  # noqa: F401
from tests.test_native_code_mode import data, native_graph
from tests.test_unified_code_mode import invoke, scripted

pytestmark = pytest.mark.timeout(40)


@tool
def echo(value: str) -> str:
    """Echo exact source text for discovery tests."""
    return value


async def test_search_native_and_mcp_have_distinct_callable_targets(tmp_path):
    remote = echo.model_copy(update={"name": "db_echo"})
    mcp = inventory(tmp_path, remote)
    result = data(
        await execute(
            native_graph(
                "return await tools.search({query:'*echo*',limit:2});",
                [echo],
                mcp=mcp,
            )
        )
    )["result"]
    assert result["total"] == 2
    assert {json.dumps(hit["target"], sort_keys=True) for hit in result["tools"]} == {
        json.dumps({"namespace": "tools", "tool_name": "echo"}, sort_keys=True),
        json.dumps({"namespace": "mcp", "server_name": "db", "tool_name": "read"}, sort_keys=True),
    }
    get_policy().tool_tiers["MCPTool"] = "deny"
    result = data(
        await execute(
            native_graph(
                "return await tools.search({query:'*echo*'});",
                [echo],
                mcp=mcp,
            )
        )
    )["result"]
    assert [hit["target"]["namespace"] for hit in result["tools"]] == ["tools"]


async def test_search_uses_current_inventory_and_excludes_direct_control_tools(tmp_path):
    mode = CodeModeMiddleware(inventory(tmp_path, echo.model_copy(update={"name": "db_echo"})))
    control = echo.model_copy(update={"name": "write_todos"})
    request = SimpleNamespace(runtime=SimpleNamespace(tools=[echo, control]))
    result = await mode._native(
        request,
        None,
        NativeInput(
            namespace="tools",
            action="search",
            query="*",
        ),
    )
    assert [hit["name"] for hit in result["tools"]] == ["echo"]
    other = SimpleNamespace(runtime=SimpleNamespace(tools=[]))
    assert (
        await mode._native(
            other,
            None,
            NativeInput(
                namespace="tools",
                action="search",
                query="*",
            ),
        )
    )["tools"] == []


async def test_search_bounds_and_unknown_contract_do_not_change_values():
    get_policy().tool_tiers["echo"] = "allow"
    body = data(
        await execute(
            native_graph(
                "const description = await tools.describe('echo'); "
                "return {contract:description.returns, value:await tools.call('echo',{value:'raw λ\\n'})};",
                [echo],
            )
        )
    )
    assert body["result"]["contract"]["status"] == "unknown"
    assert body["result"]["value"] == "raw λ\n"
    for options in ["{query:'echo',limit:0}", "{query:'echo',limit:51}", "{query:42}"]:
        message = (await execute(native_graph(f"return await tools.search({options});", [echo])))[0]
        assert message.status == "error"


def test_mcp_output_schema_survives_conversion_and_describe(tmp_path):
    mcp = inventory(tmp_path, echo.model_copy(update={"name": "db_echo"}))
    mcp.tools.clear()
    mcp._tools_cache.clear()
    schema = {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
        "required": ["values"],
    }
    converted = mcp._convert_raw_tools(
        "db",
        {"transport": "streamable_http", "url": "http://fixture.invalid/mcp"},
        [
            MCPTool(
                name="read",
                description="Read values",
                inputSchema={"type": "object"},
                outputSchema=schema,
            ),
        ],
    )
    mcp.tools.extend(converted)
    result = build_mcp_tool(mcp).invoke(
        {"action": "describe", "server_name": "db", "tool_name": "read"}
    )
    assert result["outputSchema"] == schema
    assert result["returns"]["status"] == "declared"
    converted[0].metadata.pop("nova_mcp_output_schema")
    assert (
        build_mcp_tool(mcp).invoke(
            {"action": "describe", "server_name": "db", "tool_name": "read"}
        )["returns"]["status"]
        == "unknown"
    )


async def test_real_filesystem_contracts_and_values(tmp_path):
    get_policy().tool_tiers.update({"read_file": "allow", "ls": "allow"})
    (tmp_path / "source.txt").write_bytes(b"first\nlast\n")
    fs = FilesystemMiddleware(
        backend=OptimizedFilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    )
    graph = scripted(
        [
            (
                "MCPCode",
                "const d = await tools.describe('read_file'); "
                "const text = await tools.call('read_file',{file_path:'/source.txt'}); "
                "return {contract:d.returns,text,paths:await tools.call('ls',{path:'/'})};",
            )
        ],
        middleware=[CodeModeMiddleware(), fs],
    )
    message = await invoke(graph)
    assert message.status == "success", message.content
    result = json.loads(message.content)["result"]
    assert result["contract"]["status"] == "verified"
    assert result["text"] == "first\nlast\n"
    assert result["paths"] == "['/source.txt']"
    assert all(native_returns(t)["status"] == "verified" for t in fs.tools if t.name != "delete")


async def test_oversized_selected_result_recovers_through_virtual_read_after_state_loss(tmp_path):
    get_policy().tool_tiers["read_file"] = "allow"
    saved = tmp_path / "cleared"
    fs = FilesystemMiddleware(
        backend=CompositeBackend(
            default=OptimizedFilesystemBackend(
                root_dir=str(tmp_path / "workspace"), virtual_mode=True
            ),
            routes={
                "/cleared/": OptimizedFilesystemBackend(root_dir=str(saved), virtual_mode=True)
            },
        )
    )
    mode = CodeModeMiddleware(offload_dir=saved)
    value = {"records": ["source λ\r\n" * 2000], "tail": "exact-end"}
    graph = scripted([("MCPCode", "return " + json.dumps(value) + ";")], middleware=[mode, fs])
    first = json.loads((await invoke(graph)).content)
    assert first["truncated"] is True
    assert first["output_file"]["encoding"] == "utf-8"
    path = first["output_file"]["path"]
    assert path.startswith("/cleared/")
    assert json.loads((saved / Path(path).name).read_text(encoding="utf-8")) == value
    await mode._registry.aevict(mode._fallback_thread_id)
    recovery = scripted(
        [
            (
                "MCPCode",
                "return JSON.parse(await tools.call('read_file', {file_path:"
                + json.dumps(path)
                + ",limit:10})).tail;",
            )
        ],
        middleware=[CodeModeMiddleware(), fs],
    )
    message = await invoke(recovery)
    assert message.status == "success", message.content
    assert json.loads(message.content)["result"] == "exact-end"


async def test_storage_failure_does_not_claim_recovery(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail)
    body = data(await execute(native_graph("return 'x'.repeat(13000);", offload_dir=tmp_path)))
    assert body["truncated"] is True
    assert "output_file" not in body
    assert "no recoverable file" in body["notice"]
    assert list(tmp_path.glob("*.txt")) == []


def test_offload_bytes_and_failed_write_are_never_reused(tmp_path, monkeypatch):
    original = Path.write_text

    def partial(path, *args, **kwargs):
        original(path, "incomplete", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", partial)
    assert save_tool_result(tmp_path, "call", "eval", "full\r\nλ") is None
    monkeypatch.setattr(Path, "write_text", original)
    saved = save_tool_result(tmp_path, "call", "eval", "full\r\nλ")
    assert (tmp_path / Path(saved).name).read_bytes() == "full\r\nλ".encode()


async def test_saved_json_preserves_unpaired_unicode_surrogates(tmp_path, monkeypatch):
    value = {"text": "\ud800" + "x" * 13000}

    async def selected(*args, **kwargs):
        return value, None

    monkeypatch.setattr("novacode_cli.agents.code_runtime.run_script", selected)
    body = data(await execute(native_graph("return null;", offload_dir=tmp_path)))
    path = tmp_path / Path(body["output_file"]["path"]).name
    assert json.loads(path.read_text(encoding="utf-8")) == value
