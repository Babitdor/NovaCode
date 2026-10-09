"""JSONL wire protocol between a parent Nova TUI and a spawned session process.

The child emits one JSON object per line on stdout; the parent writes one per line
to the child's stdin. Events are :mod:`novacode_cli.ui_events` dataclasses encoded
as ``{"c": "<ClassName>", "d": {<fields>}}`` and decoded back into the *same*
dataclass on the parent, so the parent's existing ``_render()`` pipeline handles a
child's output with no duplicated rendering logic.

Message envelopes
-----------------
Parent -> child::

    {"t": "prompt",          "id": "p7", "text": "...", "auto_approve": false, "images": [{"base64_data": "...", "format": "png", "placeholder": "[image]"}]}
    {"t": "interrupt_reply", "id": "i3", "result": {...}}
    {"t": "cancel"}
    {"t": "job_control", "action": "list|terminate|restart|clear|detach|logs", "job_id": 41, "request_id": "..."}
    {"t": "shutdown"}

Child -> parent::

    {"t": "ready", "session_id": "...", "thread_id": "...", "cwd": "...", ...}
    {"t": "ev", "c": "ToolCall", "d": {...}}
    {"t": "interrupt", "id": "i3", "kind": "tool", "payload": {...}}
    {"t": "turn_done", "id": "p7", "ok": true}
    {"t": "error", "message": "..."}
    {"t": "jobs", "jobs": [{"id": 41, "command": "...", "status": "running", ...}]}
    {"t": "job_logs", "request_id": "...", "output": "bounded log tail"}

Three fields need special handling; everything else in ``ui_events`` is already
JSON-safe:

* :class:`~novacode_cli.ui_events.InterruptRequest` carries an ``asyncio.Future``
  and is never encoded as an event — HITL is its own ``interrupt`` envelope.
* :class:`~novacode_cli.ui_events.FileOp` carries a
  :class:`~novacode_cli.file_ops.FileOperationRecord` (with a ``Path`` and a
  nested metrics dataclass), which the renderer reads for status/error/diff.
* :class:`~novacode_cli.ui_events.Error` carries a ``BaseException``, which is
  dropped; ``is_provider_notice`` preserves the renderer's styling decision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from novacode_cli import ui_events as ev

# Single strings larger than this are clipped before they reach the pipe. A tool
# result can be megabytes; the parent only ever shows a preview plus an expandable
# view, so shipping the whole thing would cost latency for output nobody reads.
MAX_FIELD = 256_000
_CLIP_MARKER = "… [truncated]"

# Guards against a pathological/cyclic structure in tool args recursing forever
# and taking the child down with it.
_MAX_DEPTH = 12

# Every dataclass in ui_events except InterruptRequest (which is not an event on
# the wire). Built by reflection so a newly added event type is covered for free.
_EVENT_CLASSES: dict[str, type] = {
    name: obj
    for name, obj in vars(ev).items()
    if isinstance(obj, type) and is_dataclass(obj) and name != "InterruptRequest"
}


def _clip(s: str) -> str:
    """Truncate an oversized string, marking that it was cut."""
    if len(s) <= MAX_FIELD:
        return s
    return s[: MAX_FIELD - len(_CLIP_MARKER)] + _CLIP_MARKER


def jsonable(value: Any, depth: int = 0) -> Any:
    """Coerce *value* into something ``json.dumps`` accepts, clipping long strings.

    Tool arguments and HITL payloads are arbitrary — anything that isn't a JSON
    primitive becomes its ``str()`` rather than failing the whole message, since
    the parent only displays them.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value)
    if depth >= _MAX_DEPTH:
        return _clip(str(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in value]
    return _clip(str(value))


# ── FileOperationRecord ──────────────────────────────────────────────────────


def _encode_record(rec: Any) -> dict | None:
    """Encode a ``FileOperationRecord`` (Path + nested metrics dataclass)."""
    if rec is None or not is_dataclass(rec):
        return None
    out: dict[str, Any] = {}
    for f in fields(rec):
        val = getattr(rec, f.name, None)
        if f.name == "physical_path":
            out[f.name] = str(val) if val else None
        elif f.name == "metrics":
            out[f.name] = asdict(val) if is_dataclass(val) else {}
        else:
            out[f.name] = jsonable(val)
    return out


def _decode_record(data: dict | None) -> Any:
    """Rebuild a ``FileOperationRecord``; ``None`` when the event carried none."""
    if not data:
        return None
    from novacode_cli.file_ops import FileOperationRecord, FileOpMetrics

    payload = dict(data)
    metrics_data = payload.pop("metrics", None) or {}
    physical = payload.pop("physical_path", None)

    # Tolerate version skew both ways: ignore fields this build doesn't know,
    # and let the dataclass defaults fill in fields the sender didn't send.
    known = {f.name for f in fields(FileOperationRecord)}
    payload = {k: v for k, v in payload.items() if k in known}
    known_metrics = {f.name for f in fields(FileOpMetrics)}
    metrics_data = {k: v for k, v in metrics_data.items() if k in known_metrics}

    return FileOperationRecord(
        **payload,
        physical_path=Path(physical) if physical else None,
        metrics=FileOpMetrics(**metrics_data),
    )


# ── events ───────────────────────────────────────────────────────────────────


def encode_event(event: Any) -> dict:
    """Encode a ``ui_events`` dataclass as ``{"c": name, "d": fields}``.

    Raises:
        ValueError: for ``InterruptRequest`` (it carries a Future and travels as
            its own envelope) or any object that isn't a known event.
    """
    name = type(event).__name__
    if name == "InterruptRequest":
        msg = "InterruptRequest is not encodable; send an 'interrupt' envelope instead"
        raise ValueError(msg)
    if name not in _EVENT_CLASSES:
        msg = f"not a ui_events dataclass: {name}"
        raise ValueError(msg)

    data: dict[str, Any] = {}
    for f in fields(event):
        value = getattr(event, f.name)
        if name == "FileOp" and f.name == "record":
            data[f.name] = _encode_record(value)
        elif name == "Error" and f.name == "exception":
            continue  # not serializable; is_provider_notice carries the styling
        else:
            data[f.name] = jsonable(value)
    return {"c": name, "d": data}


def decode_event(msg: dict) -> Any:
    """Rebuild the ``ui_events`` dataclass from :func:`encode_event` output.

    Returns ``None`` for an unrecognised class name rather than raising, so a
    parent talking to a child from a different Nova build skips events it doesn't
    understand instead of dying.
    """
    cls = _EVENT_CLASSES.get(msg.get("c") or "")
    if cls is None:
        return None

    data = dict(msg.get("d") or {})
    if cls.__name__ == "FileOp":
        data["record"] = _decode_record(data.get("record"))

    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


# ── framing ──────────────────────────────────────────────────────────────────


def dumps(msg: dict) -> str:
    """Serialize one message as a single newline-terminated JSON line."""
    return json.dumps(msg, ensure_ascii=False, default=str) + "\n"


def loads(line: str) -> dict | None:
    """Parse one line; ``None`` for blanks and non-JSON noise.

    A child's stdout can carry stray non-JSON output (a library printing to
    stdout during import), so unparseable lines are skipped rather than fatal.
    """
    line = line.strip()
    if not line:
        return None
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None
