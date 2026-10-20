# ruff: noqa: INP001
"""Parse approved plan steps and protect their execution checklist."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from langchain_core.messages import ToolMessage

_required_plan_steps: ContextVar[tuple[str, ...]] = ContextVar("required_plan_steps", default=())
_planning_check: ContextVar[Callable[[], bool] | None] = ContextVar(
    "plan_todo_planning", default=None
)
_STEP_SECTION = {
    "steps",
    "implementation steps",
    "execution steps",
    "implementation plan",
    "proposed approach",
}
_VERIFICATION_SECTION = {
    "verification",
    "testing",
    "validation",
    "testing & verification",
    "testing and verification",
}
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_NUMBERED_HEADING = re.compile(r"^(?:step\s+)?\d+(?:\.\d+)*[.)]?\s+(.+)$", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)(\S.*)$")


def _clean_item(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"\s+\|\s+\*\*(?:What|Why|How):\*\*.*$", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(".")


def _section_text(plan: str, names: set[str]) -> list[tuple[int, str]]:
    headings = list(_HEADING.finditer(plan))
    sections: list[tuple[int, str]] = []
    for index, heading in enumerate(headings):
        if heading.group(2).strip().lower() not in names:
            continue
        level = len(heading.group(1))
        end = len(plan)
        for next_heading in headings[index + 1 :]:
            if len(next_heading.group(1)) <= level:
                end = next_heading.start()
                break
        sections.append((level, plan[heading.end() : end]))
    return sections


def _heading_steps(body: str, parent_level: int) -> list[str]:
    steps: list[str] = []
    for heading in _HEADING.finditer(body):
        match = _NUMBERED_HEADING.match(heading.group(2))
        if match and len(heading.group(1)) > parent_level:
            item = _clean_item(match.group(1))
            if item:
                steps.append(item)
    return steps


def _list_steps(body: str) -> list[str]:
    return [
        cleaned
        for line in body.splitlines()
        if (match := _LIST_ITEM.match(line))
        if (cleaned := _clean_item(match.group(1)))
    ]


def _without_verification(plan: str) -> str:
    """Exclude verification subtrees from implementation-step discovery."""
    # Nested verification headings are still checks, not implementation steps.
    # Remove their contents before the ordered-list fallback can import them
    # again without the verification label.
    lines: list[str] = []
    verification_level: int | None = None
    for line in plan.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if verification_level is not None and level <= verification_level:
                verification_level = None
            if heading.group(2).strip().lower() in _VERIFICATION_SECTION:
                verification_level = level
        if verification_level is None:
            lines.append(line)
    return "\n".join(lines)


def _implementation_steps(plan: str) -> list[str]:
    plan = _without_verification(plan)
    for level, body in _section_text(plan, _STEP_SECTION):
        steps = _heading_steps(body, level) or _list_steps(body)
        if steps:
            return steps

    # Self-planning and older plans may put numbered step headings outside a
    # dedicated Steps section. Accept those headings, but not ordinary prose.
    fallback_steps: list[str] = []
    for heading in _HEADING.finditer(plan):
        numbered = _NUMBERED_HEADING.match(heading.group(2))
        if numbered:
            item = _clean_item(numbered.group(1))
            if item:
                fallback_steps.append(item)
    if fallback_steps:
        return fallback_steps

    # External planners often return a plain ordered list without a named
    # section. Keep these numbered actions; unnumbered bullets may be risks or
    # context, so they are intentionally excluded from this fallback.
    for line in plan.splitlines():
        match = re.match(r"^\s{0,3}\d+[.)]\s+(\S.*)$", line)
        if match:
            item = _clean_item(match.group(1))
            if item:
                fallback_steps.append(item)
    return fallback_steps


def _verification_steps(plan: str) -> list[str]:
    checks: list[str] = []
    for _level, body in _section_text(plan, _VERIFICATION_SECTION):
        for line in body.splitlines():
            item = _LIST_ITEM.match(line)
            if item:
                cleaned = _clean_item(item.group(1))
                if cleaned:
                    cleaned = re.sub(r"^(?:verify:\s*)+", "", cleaned, flags=re.IGNORECASE)
                    checks.append(f"Verify: {cleaned}")
    return checks


def build_plan_todos(plan: str) -> list[dict[str, str]]:
    """Turn plan steps and explicit checks into the agent's tracked todo state."""
    steps = _implementation_steps(plan)
    checks = _verification_steps(plan)
    items: list[str] = []
    seen: set[str] = set()
    for item in [*steps, *checks]:
        key = _todo_key(item)
        if key not in seen:
            seen.add(key)
            items.append(item)
    if not items:
        items = ["Complete the full approved plan and its verification requirements"]
    return [
        {"content": item, "status": "in_progress" if index == 0 else "pending"}
        for index, item in enumerate(items)
    ]


def _todo_key(content: str) -> str:
    """Match the same action despite presentation-only verification prefixes."""
    return re.sub(r"^(?:verify:\s*)+", "", _clean_item(content), flags=re.IGNORECASE)


@contextmanager
def activate_plan_todo_guard(
    steps: list[str],
    *,
    is_planning: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Require every imported plan item to remain in write_todos while executing."""
    token = _required_plan_steps.set(tuple(steps))
    planning_token = _planning_check.set(is_planning)
    try:
        yield
    finally:
        _planning_check.reset(planning_token)
        _required_plan_steps.reset(token)


class PlanTodoGuardMiddleware(AgentMiddleware):
    """Merge approved requirements into updates without failing the tool call."""

    async def awrap_tool_call(
        self,
        request: Any,  # noqa: ANN401
        handler: Callable[..., Awaitable[Any]],
    ) -> ToolMessage | Any:  # noqa: ANN401
        """Preserve omitted requirements and their last recorded completion state."""
        tool_call = request.tool_call
        if tool_call.get("name") != "write_todos":
            return await handler(request)

        required = _required_plan_steps.get()
        state = getattr(request, "state", {})
        planning = _planning_check.get()
        if (
            not required
            or (isinstance(state, dict) and state.get("plan_mode_enabled"))
            or (planning is not None and planning())
        ):
            return await handler(request)

        args = tool_call.get("args", {})
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("content"), str) for item in todos
        ):
            return await handler(request)  # Let the tool validate malformed arguments.
        previous = state.get("todos", []) if isinstance(state, dict) else []
        known = {
            _todo_key(item["content"]): item
            for item in (previous or [])
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        }
        proposed = {_todo_key(item["content"]): item for item in todos}
        merged = []
        required_keys = {_todo_key(step) for step in required}
        for step in required:
            key = _todo_key(step)
            item = proposed.get(key) or known.get(key) or {"status": "pending"}
            merged.append({**item, "content": step})
        merged.extend(item for item in todos if _todo_key(item["content"]) not in required_keys)
        return await handler(
            request.override(
                tool_call={
                    **tool_call,
                    "args": {**args, "todos": merged},
                }
            )
        )
