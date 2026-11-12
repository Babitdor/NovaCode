"""Coalesce accidental parallel todo snapshots before graph tool execution."""

# ruff: noqa: INP001

from typing import Any

from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import WriteTodosInput
from langchain_core.messages import AIMessage
from pydantic import ValidationError


class NovaTodoListMiddleware(TodoListMiddleware):
    """Keep distinct tasks, with the last status winning for repeated task text."""

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        """Reduce valid duplicate snapshots to one ordinary guarded tool call."""
        messages = state.get("messages", [])
        message = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if message is None:
            return None
        calls = [call for call in message.tool_calls if call["name"] == "write_todos"]
        if len(calls) < 2:  # noqa: PLR2004
            return None
        # Never turn an invalid update into a successful partial plan.
        try:
            snapshots = [WriteTodosInput.model_validate(call["args"]).todos for call in calls]
        except ValidationError:
            return super().after_model(state, runtime)
        merged = {}
        for snapshot in snapshots:
            for todo in snapshot:
                merged[todo["content"]] = todo
        final_id = calls[-1]["id"]
        replacement = message.model_copy(
            update={
                "tool_calls": [
                    {**call, "args": {"todos": list(merged.values())}}
                    if call["name"] == "write_todos"
                    else call
                    for call in message.tool_calls
                    if call["name"] != "write_todos" or call["id"] == final_id
                ]
            }
        )
        return {"messages": [replacement]}

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        """Apply the same deterministic rule to asynchronous agent graphs."""
        return self.after_model(state, runtime)
