"""Keep the task list in view, and do not stop with it half done.

Two mechanisms from how the leading coding agents run long tasks:

* **Recitation** (Manus). The todo list lives in state, but the model only sees
  it as the arguments of its last ``write_todos`` call. Once that call scrolls
  far back, or compaction summarizes it away, the model loses the plan it is
  executing. When that happens the current list is re-shown at the end of the
  system prompt. Only then: re-showing it on every call would change the
  system prompt whenever a todo changes and re-bill the cached conversation.

* **A done gate** (Codex: "do not end with in_progress/pending items"). When the
  model ends its turn while todos are still open, it is sent back once to finish
  them, or to drop a blocked one and say why. Once per user turn, so a real
  blocker still ends it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import AgentState, ModelRequest, ModelResponse
    from langgraph.runtime import Runtime

# Recite only when the last write_todos call is further back than this.
RECITE_AFTER_MESSAGES = 20

# Starts with "Internal context" so transcript replay hides it
# (core/streaming.py::is_internal_context_text) — the user did not type it.
DONE_GATE_MARKER = "Internal context: open todos."

_OPEN = ("pending", "in_progress")
_BOX = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


def _open_todos(todos: list[dict] | None) -> list[dict]:
    return [t for t in todos or [] if t.get("status") in _OPEN]


def _render(todos: list[dict]) -> str:
    return "\n".join(f"{_BOX.get(t.get('status'), '[ ]')} {t.get('content', '')}" for t in todos)


def _plan_is_out_of_view(messages: list[Any]) -> bool:
    for msg in messages[-RECITE_AFTER_MESSAGES:]:
        if isinstance(msg, AIMessage) and any(
            tc.get("name") == "write_todos" for tc in msg.tool_calls or []
        ):
            return False
    return True


def _since_last_user_turn(messages: list[Any]) -> list[Any]:
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, HumanMessage) and not str(msg.content).startswith(DONE_GATE_MARKER):
            return messages[i + 1 :]
    return messages


class TaskDisciplineMiddleware(AgentMiddleware):
    """Recite the open todo list when it is out of view; gate an unfinished stop."""

    def _with_recitation(self, request: ModelRequest) -> ModelRequest:
        todos = request.state.get("todos") if isinstance(request.state, dict) else None
        if not _open_todos(todos) or not _plan_is_out_of_view(request.messages):
            return request
        block = {
            "type": "text",
            "text": (
                "\n\n<current_todos>\nYour task list (it is no longer in recent "
                "context). Continue from the item marked [>], or the first [ ].\n"
                f"{_render(todos)}\n</current_todos>"
            ),
        }
        blocks = request.system_message.content_blocks if request.system_message else []
        return request.override(system_message=SystemMessage(content=[*blocks, block]))

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Recite the open todo list when it has scrolled out of view."""
        return handler(self._with_recitation(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async twin of :meth:`wrap_model_call`."""
        return await handler(self._with_recitation(request))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:  # noqa: ARG002
        """Send the agent back once when it stops with todos still open."""
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None  # still working
        open_items = _open_todos(state.get("todos"))
        if not open_items:
            return None
        if any(
            isinstance(m, HumanMessage) and str(m.content).startswith(DONE_GATE_MARKER)
            for m in _since_last_user_turn(messages)
        ):
            return None  # already sent back once this turn; a real blocker may end it
        nudge = (
            f"{DONE_GATE_MARKER} You ended your turn with {len(open_items)} todo(s) "
            f"still open:\n{_render(open_items)}\n\nFinish them now. If one is "
            "blocked, or the user no longer wants it, remove it with "
            "`write_todos` and tell the user why, then end your turn."
        )
        return {"messages": [HumanMessage(content=nudge)], "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """Async twin of :meth:`after_model`."""
        return self.after_model(state, runtime)
