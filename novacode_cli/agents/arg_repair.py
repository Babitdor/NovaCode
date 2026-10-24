"""Repair the common malformed-argument shapes instead of rejecting the call.

Open models (and some hosted ones) repeat a few shape mistakes that a schema
rejects although the intent is unambiguous. Rejecting costs a turn, and the raw
validator message often does not get the model to recover. Repairs run only at
the paths the validator rejected, so valid input is never changed, and the
result tells the model what was fixed:

1. ``null`` for an optional field -> the key is dropped.
2. A JSON-encoded array string (``'["a","b"]'``) where an array is expected -> parsed.
3. An empty ``{}`` placeholder where an array is expected -> ``[]``.
4. A bare string where an array is expected -> ``["value"]``.

Order matters: (2) runs before (4), or ``'["a"]'`` would become ``['["a"]']``.
Path arguments written as a Markdown auto-link (``[notes.md](http://notes.md)``)
are unwrapped to the plain path; real links pass through.
"""
# ruff: noqa: INP001, ANN401 -- existing namespace; heterogeneous tool arguments.

from __future__ import annotations

import copy
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest

logger = logging.getLogger(__name__)

MAX_NOTE_ARGS_CHARS = 400
PATH_KEYS = frozenset({"file_path", "path", "paths", "notebook_path", "directory", "cwd"})
_AUTOLINK = re.compile(r"\[([^\]\s]+)\]\(https?://([^)\s]+)\)")
_DROP = object()
_NO_REPAIR = object()


def _types(schema: dict) -> set[str]:
    """The JSON types a (possibly anyOf/oneOf) schema accepts."""
    declared = schema.get("type")
    found = set(declared if isinstance(declared, list) else [declared] if declared else [])
    for option in schema.get("anyOf", []) + schema.get("oneOf", []):
        if isinstance(option, dict):
            found |= _types(option)
    return found


def _optional(schema: dict, path: list[Any]) -> bool:
    """Whether the last key of ``path`` is not required by its object schema."""
    node = schema
    for key in path[:-1]:
        node = node.get("properties", {}).get(key, {}) if isinstance(key, str) else {}
    return isinstance(path[-1], str) and path[-1] not in node.get("required", [])


def _repaired(value: Any, schema: dict, *, optional: bool) -> tuple[Any, str]:
    if value is None and optional:
        return _DROP, "null-for-optional"
    if "array" in _types(schema):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return parsed, "json-array-parse"
            return [value], "bare-string-wrap"
        if value == {}:
            return [], "empty-placeholder"
    return _NO_REPAIR, ""


def _unlink_paths(args: dict) -> tuple[dict, bool]:
    """Unwrap ``[x](http://x)`` in path arguments, the degenerate auto-link only."""

    def unlink(text: str) -> str:
        return _AUTOLINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else m.group(0), text)

    changed = False
    out = dict(args)
    for key in PATH_KEYS & set(args):
        value = args[key]
        new = (
            unlink(value)
            if isinstance(value, str)
            else [unlink(v) if isinstance(v, str) else v for v in value]
            if isinstance(value, list)
            else value
        )
        if new != value:
            out[key] = new
            changed = True
    return out, changed


def repair_args(args: dict, schema: dict) -> tuple[dict, list[str]] | None:
    """``(repaired, repair kinds)`` when the args were invalid and are now valid, else None."""
    from jsonschema.validators import validator_for

    validator = validator_for(schema)(schema)
    args, unlinked = _unlink_paths(args)
    kinds = ["markdown-autolink"] if unlinked else []
    errors = list(validator.iter_errors(args))
    if not errors:
        return (args, kinds) if kinds else None
    fixed = copy.deepcopy(args)
    for error in errors:
        path = list(error.absolute_path)
        parent: Any = fixed
        for key in path[:-1]:
            try:
                parent = parent[key]
            except (KeyError, IndexError, TypeError):
                parent = None
                break
        if not path or parent is None:
            continue
        try:
            value = parent[path[-1]]
        except (KeyError, IndexError, TypeError):
            continue
        new, kind = _repaired(value, error.schema, optional=_optional(schema, path))
        if new is _NO_REPAIR:
            continue
        if new is _DROP:
            del parent[path[-1]]
        else:
            parent[path[-1]] = new
        kinds.append(kind)
    if not kinds or any(True for _ in validator.iter_errors(fixed)):
        return None
    return fixed, kinds


def _schema(tool: BaseTool) -> dict | None:
    schema = tool.tool_call_schema
    try:
        return schema if isinstance(schema, dict) else schema.model_json_schema()
    except Exception:  # noqa: BLE001 -- an unexportable schema is simply not repaired.
        return None


class ToolArgRepairMiddleware(AgentMiddleware):
    """Run repairable calls with fixed arguments and say what was fixed."""

    def _prepare(self, request: ToolCallRequest) -> tuple[ToolCallRequest, str] | None:
        tool = request.tool
        args = request.tool_call.get("args")
        if not isinstance(tool, BaseTool) or not isinstance(args, dict):
            return None
        schema = _schema(tool)
        repaired = repair_args(args, schema) if schema else None
        if repaired is None:
            return None
        fixed, kinds = repaired
        name = request.tool_call["name"]
        logger.info("tool_input_repaired:%s %s", name, ",".join(kinds))
        sent = json.dumps(fixed, ensure_ascii=False, default=str)
        note = (
            f"\n\n[Nova repaired this call's arguments ({', '.join(kinds)}) and ran it with "
            f"{sent[:MAX_NOTE_ARGS_CHARS]}{'…' if len(sent) > MAX_NOTE_ARGS_CHARS else ''}. "
            "Send arguments in that shape.]"
        )
        return request.override(tool_call={**request.tool_call, "args": fixed}), note

    @staticmethod
    def _noted(result: Any, note: str) -> Any:
        if isinstance(result, ToolMessage) and isinstance(result.content, str):
            return result.model_copy(update={"content": result.content + note})
        return result

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """Repair, then run the synchronous call."""
        prepared = self._prepare(request)
        if prepared is None:
            return handler(request)
        return self._noted(handler(prepared[0]), prepared[1])

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        """Repair, then run the call."""
        prepared = self._prepare(request)
        if prepared is None:
            return await handler(request)
        return self._noted(await handler(prepared[0]), prepared[1])
