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

* **A truncation gate.** A turn that ends with no tool call and either no text
  or text stopped at the output limit did not finish — the model ran out of room
  mid-answer (often mid-reasoning). Ending there silently drops the task, so it
  is sent back to continue, up to a few times in a row. Found on Terminal-Bench,
  where thinking models spent a whole capped response reasoning and returned
  nothing: the trial scored 0 with the work half done.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import AgentState, ModelRequest, ModelResponse
    from langgraph.runtime import Runtime

# Recite only when the last write_todos call is further back than this.
RECITE_AFTER_MESSAGES = 20

# Both start with "Internal context" so transcript replay hides them
# (core/streaming.py::is_internal_context_text) — the user did not type them.
DONE_GATE_MARKER = "Internal context: open todos."
CUT_OFF_MARKER = "Internal context: truncated turn."
VERIFY_GATE_MARKER = "Internal context: verification after edit."

_EDIT_TOOLS = frozenset({"write_file", "edit_file"})
_COMMAND_TOOLS = frozenset({"bash", "execute", "powershell"})
_CHECK_COMMAND = re.compile(
    r"(?:^|[;&|\n])\s*(?:(?:uv|poetry|pipenv)\s+run\s+)?"
    r"(?:(?:python(?:3(?:\.\d+)?)?|py)\s+-m\s+)?"
    r"(?:pytest|unittest|tox|nox|ruff|mypy|pyright|compileall|"
    r"cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|"
    r"gradle\s+test|make\s+(?:test|check)|"
    r"(?:npm|pnpm|yarn)\s+(?:run\s+)?test)\b",
    re.IGNORECASE,
)
_SHELL_FAILURE = re.compile(
    r"(?m)^(?:exit=[1-9]\d*\b|Exit code: [1-9]\d*\b|"
    r"\[Command failed with exit code [1-9]\d*\])"
)

# CONSECUTIVE cut-offs — the count resets as soon as the model acts (calls a
# tool). It was first counted per user turn, but an unattended run is a single
# user turn hundreds of steps long: a Terminal-Bench trial hit the cap four times
# across 129 turns, with real work in between, and the fourth ended it with time
# still on the clock. Three in a row is a model that only emits prose; three in
# an afternoon is not.
MAX_CUT_OFF_NUDGES = 3

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
    """Messages after what the USER last said.

    Skips every ``Internal context`` HumanMessage, not just this module's own:
    the skills middleware injects them too (``SUGGESTION_MARKER``), and counting
    one as a user turn would silently reset the gates' per-turn budgets.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, HumanMessage) and not str(msg.content).startswith("Internal context"):
            return messages[i + 1 :]
    return messages


def _count_marker(messages: list[Any], marker: str) -> int:
    return sum(
        1 for m in messages if isinstance(m, HumanMessage) and str(m.content).startswith(marker)
    )


def _consecutive_cut_offs(messages: list[Any]) -> int:
    """Truncation nudges sent since the model last acted or the user last spoke."""
    count = 0
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            break  # it did something: the streak is over
        if isinstance(msg, HumanMessage):
            text = str(msg.content)
            if text.startswith(CUT_OFF_MARKER):
                count += 1
            elif not text.startswith("Internal context"):
                break  # a real user turn
    return count


def _was_cut_off(msg: AIMessage) -> bool:
    """True when the turn stopped mid-answer rather than at a natural end.

    Either the response is empty, or the provider reported stopping because it
    hit the output limit (``length``; Ollama spells it ``done_reason``).
    """
    if msg.tool_calls:
        return False
    if not msg.text.strip():
        return True
    meta = msg.response_metadata or {}
    return (meta.get("done_reason") or meta.get("finish_reason")) == "length"


def _check_failed(result: ToolMessage) -> bool:
    """Use a structured outcome when available; recognize shell failure wrappers."""
    if result.status == "error":
        return True
    artifact = result.artifact if isinstance(result.artifact, dict) else {}
    exit_code = artifact.get("exit_code")
    if isinstance(exit_code, int):
        return exit_code != 0
    if artifact.get("execution_outcome") == "failure":
        return True
    return bool(_SHELL_FAILURE.search(str(result.content)))


def _verification_gap(messages: list[Any]) -> str | None:
    """Return a missing or failed check after the last observed file edit.

    Shell writes and arbitrary custom test scripts cannot be identified reliably
    from the transcript. A successful command is not proof of check relevance.
    """
    results = {msg.tool_call_id: msg for msg in messages if isinstance(msg, ToolMessage)}
    last_edit = -1
    last_check = -1
    check_failed = False
    for index, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        for call in msg.tool_calls or []:
            name = call.get("name")
            call_id = call.get("id")
            result = results.get(call_id) if isinstance(call_id, str) else None
            if name in _EDIT_TOOLS and result and result.status != "error":
                last_edit = index
            elif name in _COMMAND_TOOLS:
                command = (call.get("args") or {}).get("command", "")
                if (
                    isinstance(command, str)
                    and _CHECK_COMMAND.search(command)
                    and result is not None
                ):
                    last_check = index
                    check_failed = _check_failed(result)
    if last_edit < 0:
        return None
    if last_check <= last_edit:
        return "missing"
    return "failed" if check_failed else None


class TaskDisciplineMiddleware(AgentMiddleware):
    """Recite the open todo list when it is out of view; gate an unfinished stop."""

    def __init__(self, *, verify_after_edit: bool = False) -> None:
        """Enable the experimental post-edit reminder only when requested."""
        self.verify_after_edit = verify_after_edit

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
        """Send the agent back when a turn was cut off, or stopped with todos open."""
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None  # still working

        this_turn = _since_last_user_turn(messages)
        if _was_cut_off(last) and _consecutive_cut_offs(messages) < MAX_CUT_OFF_NUDGES:
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            f"{CUT_OFF_MARKER} Your last response was empty or stopped at "
                            "the output limit before you acted, so the task is unfinished. "
                            "Continue from where you left off: keep reasoning brief, act by "
                            "calling tools, and build large outputs with a script or in "
                            "several smaller writes rather than in one message."
                        )
                    )
                ],
                "jump_to": "model",
            }

        gap = _verification_gap(this_turn) if self.verify_after_edit else None
        if gap and not _count_marker(this_turn, VERIFY_GATE_MARKER):
            reason = (
                "Your last recognized check after the edit failed. Inspect its result, "
                "fix the cause, and rerun a focused check. "
                if gap == "failed"
                else "You edited a file and ended this turn without an observed completed "
                "check command after the last edit. "
            )
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            f"{VERIFY_GATE_MARKER} {reason}"
                            "If you already ran a relevant check using a command this "
                            "detector did not recognize, cite the command and its result. "
                            "Otherwise run a focused check and inspect its result. If "
                            "verification is unavailable, explain what you tried and "
                            "what remains unverified."
                        )
                    )
                ],
                "jump_to": "model",
            }

        open_items = _open_todos(state.get("todos"))
        if not open_items:
            return None
        if _count_marker(this_turn, DONE_GATE_MARKER):
            return None  # already sent back once this turn; a real blocker may end it
        nudge = (
            f"{DONE_GATE_MARKER} You ended your turn with {len(open_items)} todo(s) "
            f"still open:\n{_render(open_items)}\n\nFinish them now. If one is "
            "blocked, or the user no longer wants it, remove it with "
            "`write_todos` and tell the user why, then end your turn."
        )
        return {"messages": [HumanMessage(content=nudge)], "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Async twin of :meth:`after_model`."""
        return self.after_model(state, runtime)
