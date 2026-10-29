"""Pinned upstream Ponytail instructions, without a Node runtime dependency."""

# ruff: noqa: INP001 -- existing agents namespace.

from __future__ import annotations

import re
from pathlib import Path

PONYTAIL_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "ponytail"
PONYTAIL_LEVELS = ("lite", "full", "ultra")
_RUNTIME_MODES = ("off", *PONYTAIL_LEVELS)


def skill_body(name: str = "ponytail") -> str:
    """Read a bundled skill, preserving its body verbatim."""
    if name not in {
        "ponytail",
        *("ponytail-" + n for n in ("review", "audit", "debt", "gain", "help")),
    }:
        message = "Unknown Ponytail skill"
        raise ValueError(message)
    text = (PONYTAIL_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    return re.sub(r"^---[\s\S]*?---\s*", "", text, count=1)


def ponytail_instructions(level: str = "full") -> str:
    """Match the pinned upstream getPonytailInstructions builder exactly."""
    mode = level.strip().lower() if isinstance(level, str) else "full"
    if mode == "review":
        return "PONYTAIL MODE ACTIVE — level: review. Behavior defined by /ponytail-review skill."
    if mode not in _RUNTIME_MODES:
        mode = "full"
    lines = []
    for line in skill_body().split("\n"):
        match = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|", line) or re.match(r'^-\s*([^:]+):\s*"', line)
        label = match[1].strip().lower() if match else None
        if label in _RUNTIME_MODES and label != mode:
            continue
        lines.append(line)
    return f"PONYTAIL MODE ACTIVE — level: {mode}\n\n" + "\n".join(lines)


def is_deactivation_command(text: str) -> bool:
    """Only standalone commands disable the mode, matching upstream hooks."""
    return re.sub(r"[.!?\s]+$", "", text.strip().lower()) in {"stop ponytail", "normal mode"}


def helper_prompt(text: str) -> str:
    """Expand an explicitly requested helper without changing the active level."""
    parts = text.split(maxsplit=1)
    name = parts[0].removeprefix("/").lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    return (
        skill_body(name)
        + "\n\n"
        + args
        + (
            "\nNova host: compression settings use /lite-mode and the composer selector. "
            "Claude-specific band/board and statusline hooks are not installed in Nova. "
            "Report measured savings only; do not present upstream benchmark figures "
            "as this session's usage."
            if name in {"ponytail-help", "ponytail-gain"}
            else ""
        )
    )
