"""Malformed but unambiguous tool arguments are repaired at the rejected path only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from novacode_cli.agents.arg_repair import ToolArgRepairMiddleware, _schema, repair_args


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
