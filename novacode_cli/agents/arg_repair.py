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
        message = error.message
        if error.validator == "additionalProperties":
            # Name the real properties instead of dumping the schema blob:
            # the model hallucinated a parameter from another tool's dialect.
            allowed = sorted((error.schema.get("properties") or {}).keys())
            if allowed:
                unexpected = re.findall(r"'([^']+)' was unexpected", message)
                near = _nearest(unexpected[0], allowed) if unexpected else None
                hint = f" (allowed: {', '.join(allowed[:8])})"
                if near:
                    hint = f" (did you mean '{near}'? allowed: {', '.join(allowed[:8])})"
                message = f"{message.split(';')[0].strip()}{hint}"
        parts.append(f"- {path}: {message}")
    if len(errors) > MAX_ISSUE_LINES:
        parts.append(f"(and {len(errors) - MAX_ISSUE_LINES} more issues)")
    return "\n".join(parts)


def _nearest(name: str, candidates: list[str]) -> str | None:
    """The closest candidate by token overlap or edit distance, if close enough."""
    import difflib

    match = difflib.get_close_matches(name, candidates, n=1, cutoff=0.4)
    if match:
        return match[0]
    name_words = set(re.split(r"[_-]", name.lower()))
    for candidate in sorted(candidates):
        if name_words & set(re.split(r"[_-]", candidate.lower())):
            return candidate
    return None


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


def repair_invalid(
    args: dict, schema: dict, tool_name: str | None, errors: list
) -> tuple[dict, list[str], list[str]] | None:
    """Shape + relational repairs for already-collected validator errors.

    Returns ``(fixed, kinds, notes)`` when the repaired args validate clean,
    else ``None``. Callers keep their own acceptance validator: the repaired
    args must pass the original gate, not just this walk.
    """
    from jsonschema.validators import validator_for

    validator = validator_for(schema)(schema)
    fixed = copy.deepcopy(args)
    kinds: list[str] = []
    notes: list[str] = []

    def node_at(path: list[Any]) -> Any:
        node: Any = fixed
        for key in path:
            try:
                node = node[key]
            except (KeyError, IndexError, TypeError):
                return None
        return node

    for error in errors:
        path = list(error.absolute_path)
        # Over-max numbers clamp to the schema maximum: the model asked for
        # more than the tool gives (tools:list limit=60 with a cap of 50).
        # Reading fewer records is recoverable; rejecting is not.
        if error.validator == "maximum" and path:
            node = node_at(path[:-1])
            if isinstance(node, dict):
                key = path[-1]
                value = node.get(key)
                if isinstance(value, (int, float)) and value > error.validator_value:
                    node[key] = error.validator_value
                    kinds.append("value-clamped")
                    notes.append(f"{key} clamped to {error.validator_value}")
            continue
        # A required property the schema gives a default for: fill it. The
        # server declared that default itself, so the fill matches what the
        # server would have chosen (playwright's screenshot scale, …).
        if error.validator == "required":
            properties = error.schema.get("properties") or {}
            node = node_at(path) if path else fixed
            if isinstance(node, dict):
                for key in error.validator_value if isinstance(error.validator_value, list) else []:
                    if key in node:
                        continue
                    default = (properties.get(key) or {}).get("default", _NO_REPAIR)
                    if default is not _NO_REPAIR:
                        node[key] = default
                        kinds.append("default-filled")
                        notes.append(f"{key} filled with the schema default")
            continue
        # An enum/const violation where the schema declares a default: the
        # model guessed a value from another dialect ('topic') and the tool's
        # own default is the honest substitute.
        if error.validator in {"enum", "const"} and path:
            default = error.schema.get("default", _NO_REPAIR)
            node = node_at(path[:-1])
            if default is not _NO_REPAIR and isinstance(node, dict):
                node[path[-1]] = default
                kinds.append("default-filled")
                notes.append(f"{path[-1]} reset to the schema default")
            continue
        if not path or node_at(path[:-1]) is None:
            continue
        parent = node_at(path[:-1])
        if not isinstance(parent, dict):
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
    if (
        remaining
        and tool_name is not None
        and _apply_relational_defaults(fixed, remaining, tool_name, kinds, notes)
    ):
        remaining = list(validator.iter_errors(fixed))
    if remaining:
        return None
    return fixed, kinds, notes


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
    repaired = repair_invalid(args, schema, tool_name, errors)
    if repaired is None:
        return _compact_issues(errors)
    fixed, kinds, notes = repaired
    return (fixed, kinds), tuple(notes)


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
