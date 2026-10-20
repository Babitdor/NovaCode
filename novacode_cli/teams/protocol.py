"""Versioned lossless team control frames alongside the existing event protocol."""

from __future__ import annotations

import json
from typing import Any

from novacode_cli.teams.models import TeamError

VERSION = 1
MAX_CONTROL_BYTES = 1024 * 1024


def encode_control(value: dict[str, Any]) -> str:
    """Reject oversized control fields; never clip identity or approval arguments."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
    if len(encoded.encode()) > MAX_CONTROL_BYTES:
        msg = "Control frame limit exceeded"
        raise TeamError(msg)
    return encoded


def validate_handshake(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """Validate exact worker, model, workspace, attempt and policy before dispatch."""
    for field in (
        "version",
        "agent",
        "team",
        "project",
        "root",
        "attempt",
        "model",
        "provider",
        "cwd",
        "policy",
        "base_revision",
    ):
        if field not in actual or actual[field] != expected.get(field):
            msg = f"Worker handshake mismatch: {field}"
            raise TeamError(msg)
    missing = set(expected.get("required_tools", [])) - set(actual.get("capabilities", []))
    if missing:
        message = "Worker handshake lacks configured capabilities"
        raise TeamError(message)
    if actual["version"] != VERSION:
        msg = "Unsupported worker protocol"
        raise TeamError(msg)
