"""Malformed but unambiguous tool arguments are repaired at the rejected path only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from novacode_cli.agents.arg_repair import (
    ToolArgRepairMiddleware,
    _schema,
    evaluate_args,
    repair_args,
)


class _Args(BaseModel):
    paths: list[str]
    pattern: str
    timeout: int | None = None
    limit: int = 5


TOOL = StructuredTool.from_function(
    lambda **_kwargs: "ok", name="search", args_schema=_Args, description="Search files."
)
SCHEMA = _schema(TOOL)


@pytest.mark.parametrize(
    ("args", "expected", "kind"),
    [
        (
            {"paths": '["a","b"]', "pattern": "x"},
            {"paths": ["a", "b"], "pattern": "x"},
            "json-array-parse",
        ),
        (
            {"paths": "a.py", "pattern": "x"},
            {"paths": ["a.py"], "pattern": "x"},
            "bare-string-wrap",
        ),
        ({"paths": {}, "pattern": "x"}, {"paths": [], "pattern": "x"}, "empty-placeholder"),
        (
            {"paths": ["a"], "pattern": "x", "limit": None},
            {"paths": ["a"], "pattern": "x"},
            "null-for-optional",
        ),
    ],
)
def test_each_repair(args: dict, expected: dict, kind: str):
    assert repair_args(args, SCHEMA) == (expected, [kind])


def test_valid_and_unrepairable_args_are_left_alone():
    assert repair_args({"paths": ["a"], "pattern": "x", "timeout": None}, SCHEMA) is None
    assert repair_args({"paths": 5, "pattern": "x"}, SCHEMA) is None
    # A missing required field is not guessed.
    assert repair_args({"paths": "a"}, SCHEMA) is None


def test_evaluate_args_tries_an_object_string_where_an_object_is_expected():
    """The object sibling of json-array-parse: a JSON-encoded object string."""
    tool = StructuredTool.from_function(
        lambda **_kwargs: "ok",
        name="search",
        args_schema=type(
            "_OptsArgs",
            (BaseModel,),
            {"__annotations__": {"paths": list[str], "pattern": str, "options": dict | None}},
        ),
        description="Search files.",
    )
    verdict = evaluate_args(
        {"paths": ["a"], "pattern": "x", "options": '{"case": false}'}, _schema(tool), "search"
    )
    (fixed, kinds), _ = verdict
    assert fixed["options"] == {"case": False}
    assert "json-object-parse" in kinds


def test_unrepairable_args_get_a_compact_model_readable_summary():
    """Instead of the raw validator blob, the model gets a few readable lines."""
    summary = evaluate_args({"paths": 5}, SCHEMA, "search")
    assert isinstance(summary, str)
    assert "paths" in summary and "not of type" in summary
    assert "Required: pattern" in summary or "pattern" in summary


async def test_unrepairable_args_return_a_model_readable_message_not_the_handler():
    """Unrepairable invalid args: the handler never runs and the model gets a
    compact summary instead of the raw validator blob."""
    request = SimpleNamespace(
        tool=TOOL, tool_call={"id": "9", "name": "search", "args": {"paths": 5}}
    )
    request.override = lambda **changes: SimpleNamespace(**{**vars(request), **changes})
    handler = AsyncMock(return_value=ToolMessage(content="never", tool_call_id="9"))
    result = await ToolArgRepairMiddleware().awrap_tool_call(request, handler)
    handler.assert_not_called()
    assert "did not match its schema" in result.content
    assert "paths" in result.content
    assert result.status == "error"


def test_relational_default_fills_the_missing_pair_when_the_partner_is_present():
    """read_file with limit but no offset: the pair is incomplete, not invalid."""
    tool = StructuredTool.from_function(
        lambda **_kwargs: "ok",
        name="read_file",
        args_schema=type(
            "_ReadArgs",
            (BaseModel,),
            {"__annotations__": {"file_path": str, "offset": int, "limit": int}},
        ),
        description="Read a file.",
    )
    schema = _schema(tool)
    verdict = evaluate_args({"file_path": "x.py", "limit": 30}, schema, "read_file")
    (fixed, kinds), note_extra = verdict
    assert fixed["offset"] == 0
    assert "relational-default" in kinds
    assert any("defaulted to 0" in note for note in note_extra)

    # The inverse: offset alone -> the backend's default limit.
    verdict = evaluate_args({"file_path": "x.py", "offset": 10}, schema, "read_file")
    (fixed, kinds), note_extra = verdict
    assert fixed["limit"] == 2000
    assert any("defaulted to 2000" in note for note in note_extra)

    # The same fill for a DIFFERENT tool is not guessed: the pair is
    # read_file's contract, not a generic schema shape.
    verdict = evaluate_args({"file_path": "x.py", "limit": 30}, schema, "grep_file")
    assert isinstance(verdict, str), (
        "the read_file relational default must not fire for other tools"
    )


def test_degenerate_markdown_autolink_in_a_path_is_unwrapped_only():
    args = {"paths": ["/proj/[notes.md](http://notes.md)", "[docs](https://x.com)"], "pattern": "x"}
    fixed, kinds = repair_args(args, SCHEMA)
    assert fixed["paths"] == ["/proj/notes.md", "[docs](https://x.com)"]
    assert kinds == ["markdown-autolink"]


async def test_middleware_runs_repaired_call_and_tells_the_model():
    request = SimpleNamespace(
        tool=TOOL,
        tool_call={"id": "1", "name": "search", "args": {"paths": "a.py", "pattern": "x"}},
    )
    request.override = lambda **changes: SimpleNamespace(**{**vars(request), **changes})
    handler = AsyncMock(return_value=ToolMessage(content="found 2", tool_call_id="1"))
    result = await ToolArgRepairMiddleware().awrap_tool_call(request, handler)
    assert handler.call_args.args[0].tool_call["args"] == {"paths": ["a.py"], "pattern": "x"}
    assert result.content.startswith("found 2")
    assert "bare-string-wrap" in result.content
    assert '{"paths": ["a.py"], "pattern": "x"}' in result.content

    valid = SimpleNamespace(
        tool=TOOL, tool_call={"id": "2", "name": "search", "args": {"paths": ["a"], "pattern": "x"}}
    )
    handler.reset_mock()
    await ToolArgRepairMiddleware().awrap_tool_call(valid, handler)
    assert handler.call_args.args[0] is valid
