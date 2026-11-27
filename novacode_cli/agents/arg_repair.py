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
5. A JSON-encoded object string (``'{"a":1}'``) where an object is expected -> parsed.

Order matters: (2) runs before (4), or ``'["a"]'`` would become ``['["a"]']``.
Path arguments written as a Markdown auto-link (``[notes.md](http://notes.md)``)
are unwrapped to the plain path; real links pass through.

Two things input repair cannot fix, handled separately:

- **Unrepairable invalid args** return a compact, model-readable summary of the
  validator's issues instead of the raw blob (logged ``tool_input_invalid``).
- **Relational invariants** (each field valid, the pair incomplete) are taught
  the intent: for ``read_file``, ``limit`` alone -> ``offset = 0`` and
  ``offset`` alone -> ``limit = 2000`` — surfaced in the result with no error
  prefix, so the model sees what was picked and can self-correct.
"""
# ruff: noqa: INP001, ANN401 -- existing namespace; heterogeneous tool arguments.

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from novacode_cli.core.tool_results import append_display_note

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest

logger = logging.getLogger(__name__)

MAX_NOTE_ARGS_CHARS = 400
MAX_ISSUE_LINES = 5
PATH_KEYS = frozenset({"file_path", "path", "paths", "notebook_path", "directory", "cwd"})
_AUTOLINK = re.compile(r"\[([^\]\s]+)\]\(https?://([^)\s]+)\)")
_REQUIRED = re.compile(r"'([^']+)' is a required property")
_DROP = object()
_NO_REPAIR = object()

#: Relational invariants: (tool, missing param) -> (present partner, default the
#: missing param takes, surfaced note). Applied only when the partner is provided.
_RELATIONAL_DEFAULTS: dict[tuple[str, str], tuple[str, object, str]] = {
    ("read_file", "limit"): (
        "offset",
        2000,
        "Note: limit was not provided; defaulted to 2000 lines. To read more or "
        "fewer lines, retry with both offset and limit.",
    ),
    ("read_file", "offset"): (
        "limit",
        0,
        "Note: offset was not provided; defaulted to 0 (the start of the file).",
    ),
}


@dataclasses.dataclass(frozen=True)
class ArgVerdict:
    """The outcome of checking one call's arguments against its schema.

    - ``invalid_summary is None``: run ``fixed`` (``kinds``/``note_extra`` say
      what was adjusted; empty when the args were valid as-is).
    - ``invalid_summary`` set: the args are invalid and unrepairable — a
      compact, model-readable summary to return instead of the raw blob.
    """

    fixed: dict
    kinds: tuple[str, ...] = ()
    note_extra: tuple[str, ...] = ()
    invalid_summary: str | None = None

    @property
    def adjusted(self) -> bool:
        """Whether anything changed (or was chosen) and the model should hear it."""
        return self.invalid_summary is None and bool(self.kinds or self.note_extra)


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
    if "object" in _types(schema) and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed, "json-object-parse"
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


def _compact_issues(errors: list) -> str:
    """The validator's issues as a few model-readable lines, not a raw blob."""
    parts = []
    for error in errors[:MAX_ISSUE_LINES]:
        path = ".".join(str(key) for key in error.absolute_path) or "(root)"
        parts.append(f"- {path}: {error.message}")
    if len(errors) > MAX_ISSUE_LINES:
        parts.append(f"(and {len(errors) - MAX_ISSUE_LINES} more issues)")
    return "\n".join(parts)


def _apply_relational_defaults(
    fixed: dict, errors: list, tool_name: str, kinds: list[str], notes: list[str]
) -> bool:
    """Fill a missing parameter whose pair is present and whose default is known.

    Each field is independently valid; the invariant is in the relationship, so
    input repair cannot express it — the model's intent is honoured instead, and
    the choice is surfaced (no error prefix) so the model can self-correct.
    """
    changed = False
    for error in errors:
        if error.validator != "required":
            continue
        match = _REQUIRED.search(error.message)
        if not match:
            continue
        rule = _RELATIONAL_DEFAULTS.get((tool_name, match.group(1)))
        if rule is None:
            continue
        partner, default, note = rule
        if partner not in fixed:
            continue
        fixed[match.group(1)] = default
        kinds.append("relational-default")
        notes.append(note)
        changed = True
    return changed


def evaluate_args(
    args: dict, schema: dict, tool_name: str | None = None
) -> tuple[dict, list[str]] | str | None:
    """Check one call's arguments against the tool's JSON schema.

    Returns:
        - ``None``: the args are valid as-is — run them untouched.
        - ``(fixed, kinds)``: the args needed adjustment (shape repairs, an
          autolink unwrap, a relational default) — run ``fixed`` and surface
          the note.
        - ``str``: the args are invalid and unrepairable — a compact,
          model-readable summary of the issues, to return instead of the raw
          validator blob.
    """
    from jsonschema.validators import validator_for

    validator = validator_for(schema)(schema)
    args, unlinked = _unlink_paths(args)
    kinds = ["markdown-autolink"] if unlinked else []
    errors = list(validator.iter_errors(args))
    if not errors:
        return ((args, kinds), ()) if kinds else None
    fixed = copy.deepcopy(args)
    notes: list[str] = []
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
    remaining = list(validator.iter_errors(fixed))
    if remaining and tool_name is not None and _apply_relational_defaults(
        fixed, remaining, tool_name, kinds, notes
    ):
        remaining = list(validator.iter_errors(fixed))
    if not remaining:
        return (fixed, kinds), tuple(notes)
    return _compact_issues(remaining)


def repair_args(args: dict, schema: dict) -> tuple[dict, list[str]] | None:
    """``(repaired, repair kinds)`` when the args were invalid and are now valid, else None."""
    outcome = evaluate_args(args, schema)
    if outcome is None or isinstance(outcome, str):
        return None
    (fixed, kinds), _note_extra = outcome
    return fixed, kinds


def _schema(tool: BaseTool) -> dict | None:
    schema = tool.tool_call_schema
    try:
        return schema if isinstance(schema, dict) else schema.model_json_schema()
    except Exception:  # noqa: BLE001 -- an unexportable schema is simply not repaired.
        return None


class ToolArgRepairMiddleware(AgentMiddleware):
    """Run repairable calls with fixed arguments and say what was fixed.

    Unrepairable invalid arguments get a compact, model-readable summary of
    the validator's issues instead of the raw blob, and are logged
    ``tool_input_invalid`` so repair rates per (model, tool) are observable.
    """

    def _prepare(
        self, request: ToolCallRequest
    ) -> tuple[ToolCallRequest, str] | ToolMessage | None:
        tool = request.tool
        args = request.tool_call.get("args")
        if not isinstance(tool, BaseTool) or not isinstance(args, dict):
            return None
        name = request.tool_call["name"]
        schema = _schema(tool)
        outcome = evaluate_args(args, schema, name) if schema else None
        if outcome is None:
            return None
        if isinstance(outcome, str):
            logger.info("tool_input_invalid:%s", name)
            return ToolMessage(
                content=(
                    f"The arguments for `{name}` did not match its schema:\n{outcome}\n"
                    f"Re-send the call with arguments matching the schema above."
                ),
                tool_call_id=request.tool_call.get("id", ""),
                name=name,
                status="error",
            )
        (fixed, kinds), note_extra = outcome
        logger.info("tool_input_repaired:%s %s", name, ",".join(kinds))
        sent = json.dumps(fixed, ensure_ascii=False, default=str)
        note = (
            f"\n\n[Nova repaired this call's arguments ({', '.join(kinds)}) and ran it with "
            f"{sent[:MAX_NOTE_ARGS_CHARS]}{'…' if len(sent) > MAX_NOTE_ARGS_CHARS else ''}. "
            "Send arguments in that shape.]"
        )
        for extra in note_extra:
            note += f"\n\n[{extra}]"
        return request.override(tool_call={**request.tool_call, "args": fixed}), note

    @staticmethod
    def _noted(result: Any, note: str) -> Any:
        if isinstance(result, ToolMessage) and isinstance(result.content, str):
            return append_display_note(result, note)
        return result

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """Repair, then run the synchronous call."""
        prepared = self._prepare(request)
        if prepared is None:
            return handler(request)
        if isinstance(prepared, ToolMessage):
            return prepared
        return self._noted(handler(prepared[0]), prepared[1])

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        """Repair, then run the call."""
        prepared = self._prepare(request)
        if prepared is None:
            return await handler(request)
        if isinstance(prepared, ToolMessage):
            return prepared
        return self._noted(await handler(prepared[0]), prepared[1])
