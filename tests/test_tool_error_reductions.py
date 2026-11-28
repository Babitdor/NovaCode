"""Regression tests for the tool-call error reductions from session 1b05d1f5."""

from __future__ import annotations

import json

from novacode_cli.agents.arg_repair import evaluate_args
from novacode_cli.mcp.generic import MCPInput, mcp_route_hint, resolve_mcp_route
from novacode_cli.session.history_export import export_markdown
from novacode_cli.session.history_tree import HistoryTree


class _Middleware:
    """The slice of MCPMiddleware resolve_mcp_route reads."""

    def __init__(self, tools_cache, tools):
        self._tools_cache = tools_cache
        self.tools = tools


def test_oversized_numeric_argument_clamps_to_the_schema_maximum():
    schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "maximum": 50}},
        "required": ["limit"],
    }
    outcome = evaluate_args({"limit": 60}, schema, "tools_list")
    assert outcome is not None and not isinstance(outcome, str)
    (fixed, kinds), notes = outcome
    assert fixed == {"limit": 50}
    assert "value-clamped" in kinds
    assert any("clamped to 50" in note for note in notes)


def test_missing_required_property_fills_the_schema_default():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "scale": {"type": "string", "default": "css"}},
        "required": ["path", "scale"],
    }
    outcome = evaluate_args({"path": "/x.png"}, schema, "browser_take_screenshot")
    assert outcome is not None and not isinstance(outcome, str)
    (fixed, kinds), notes = outcome
    assert fixed == {"path": "/x.png", "scale": "css"}
    assert "default-filled" in kinds
    assert any("scale" in note for note in notes)


def test_enum_violation_resets_to_the_schema_default():
    schema = {
        "type": "object",
        "properties": {
            "memory_type": {"enum": ["user", "project"], "default": "user"},
            "path": {"type": "string"},
        },
        "required": ["memory_type"],
    }
    outcome = evaluate_args({"memory_type": "topic", "path": "/memories/x.md"}, schema, "write_memory")
    assert outcome is not None and not isinstance(outcome, str)
    (fixed, kinds), _notes = outcome
    assert fixed["memory_type"] == "user"
    assert "default-filled" in kinds


def test_unknown_property_error_names_the_real_properties():
    schema = {
        "type": "object",
        "properties": {"glob": {"type": "string"}, "path": {"type": "string"}},
        "additionalProperties": False,
        "required": ["path"],
    }
    outcome = evaluate_args({"path": "x", "ignoreCase": True}, schema, "grep")
    assert isinstance(outcome, str)
    assert "did you mean" not in outcome or "allowed" in outcome
    assert "glob" in outcome and "path" in outcome
    assert "ignoreCase" in outcome


def test_mcp_double_prefix_resolves_with_a_note():
    tool = type("T", (), {"name": "playwright_browser_navigate", "metadata": {"nova_mcp_server": "playwright"}})()
    middleware = _Middleware(
        tools_cache=[
            {"server": "playwright", "skill_alias": "browser_navigate", "name": "playwright_browser_navigate"}
        ],
        tools=[tool],
    )
    params = MCPInput.model_validate(
        {"action": "call", "server_name": "playwright", "tool_name": "playwright_browser_navigate"}
    )
    target, note = resolve_mcp_route(middleware, params)
    assert target is tool
    assert note and "browser_navigate" in note and "already prefixed" in note


def test_mcp_unknown_tool_gets_a_did_you_mean_hint():
    middleware = _Middleware(
        tools_cache=[
            {"server": "playwright", "skill_alias": "browser_navigate", "name": "playwright_browser_navigate"},
            {"server": "playwright", "skill_alias": "browser_click", "name": "playwright_browser_click"},
        ],
        tools=[],
    )
    params = MCPInput.model_validate(
        {"action": "call", "server_name": "playwright", "tool_name": "browser_navgiate"}
    )
    hint = mcp_route_hint(middleware, params)
    assert "browser_navigate" in hint


def test_markdown_export_writes_a_tool_result_once(tmp_path):
    """The export used to write a ToolMessage body twice: once as the node's
    bare text and again as the Result block — doubling every Code Mode result."""

    tree = HistoryTree(tmp_path)
    tree.sync(
        [
            {"type": "ai", "content": "working", "id": "assistant-1"},
            {
                "type": "ToolMessage",
                "name": "MCPCode",
                "tool_call_id": "c1",
                "content": json.dumps({"result": "BODY-MARKER", "calls": 1}),
                "id": "tool-1",
            },
        ]
    )
    page = export_markdown(tree, title="t", subtitle="s")
    assert page.count("BODY-MARKER") == 1, "the tool result body was written twice"