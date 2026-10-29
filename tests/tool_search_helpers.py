"""Explicit runtime inventories and correlated search results for offline tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from novacode_cli.agents.tool_search import ToolSearchMiddleware, _description, _name
from novacode_cli.agents.tool_search_variants import BM25_TOOL, REGEX_TOOL

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest


class SearchText(str):  # noqa: SLOT000 - Retain a structured artifact on this test string.
    """Readable search output retaining its structured result for assertions."""

    def __new__(cls, message: ToolMessage) -> Self:
        """Retain the artifact without changing readable string comparisons."""
        assert isinstance(message.content, str)
        value = super().__new__(cls, message.content)
        value.artifact = message.artifact
        return value


def history(
    result: SearchText,
    *,
    name: str = "search_tools",
    tool_call_id: str = "search",
) -> list[AIMessage | ToolMessage]:
    """A real request/result pair; orphan outputs deliberately cannot load schemas."""
    return [
        AIMessage("", tool_calls=[{"name": name, "args": {}, "id": tool_call_id}]),
        ToolMessage(str(result), artifact=result.artifact, name=name, tool_call_id=tool_call_id),
    ]


class SearchHarness(ToolSearchMiddleware):
    """Remember test inputs so each search explicitly supplies its owning runtime."""

    def __init__(self, **kwargs: Any) -> None:
        """Start with an explicit empty inventory."""
        super().__init__(**kwargs)
        self.test_tools: list = []
        self.test_messages: list = []

    def _apply(self, request: ModelRequest) -> ModelRequest:
        self.test_tools = request.tools
        self.test_messages = request.messages
        return super()._apply(request)

    def invoke(self, arguments: dict, *, name: str = "search_tools") -> SearchText:
        """Call the registered tool with an explicit runtime and tool-call ID."""
        call_id = uuid4().hex
        runtime = ToolRuntime(
            state={"messages": self.test_messages},
            context=None,
            config={},
            stream_writer=lambda _: None,
            tool_call_id=call_id,
            store=None,
            tools=[
                tool
                if isinstance(tool, BaseTool)
                else StructuredTool.from_function(
                    lambda: "",
                    name=_name(tool),
                    description=_description(tool),
                )
                for tool in self.test_tools
            ],
        )
        tool = next(tool for tool in self.tools if tool.name == name)
        response = tool.invoke(
            {
                "name": name,
                "id": call_id,
                "type": "tool_call",
                "args": {**arguments, "runtime": runtime},
            }
        )
        assert isinstance(response, ToolMessage)
        return SearchText(response)

    def text(self, query: str, limit: int = 5, offset: int = 0) -> SearchText:
        """Read a default search result."""
        return self.invoke({"query": query, "limit": limit, "offset": offset})

    def regex(self, query: str, limit: int = 5, offset: int = 0) -> SearchText:
        """Read a regex search result."""
        return self.invoke({"query": query, "limit": limit, "offset": offset}, name=REGEX_TOOL)

    def bm25(self, query: str, limit: int = 5, offset: int = 0) -> SearchText:
        """Read a keyword search result."""
        return self.invoke({"query": query, "limit": limit, "offset": offset}, name=BM25_TOOL)
