"""Selectable status-line fields, free of any UI framework.

The list lives here rather than in `novacode_cli/tui/statusline.py` because the
socket server and the ratatui client must offer the same choices without
importing Textual (ADR-003). The Textual module re-exports these names, so both
frontends read one definition and neither can drift.
"""

from __future__ import annotations

FIELDS: dict[str, str] = {
    "activity": "Activity and elapsed time",
    "model": "Model",
    "directory": "Current directory",
    "branch": "Git branch",
    "session": "Session name",
    "version": "Nova version",
    "phase": "Planning / implementation phase",
    "permissions": "Permission mode",
    "context": "Context usage",
    "cache": "Prompt cache",
    "learning": "Learning status",
    "background": "Background tasks",
    "bridges": "Remote connections",
    "notifications": "Notifications and pending approvals",
    "files": "Instruction file count",
    "skills": "Enabled skill count",
}
DEFAULT_FIELDS: tuple[str, ...] = (
    "activity",
    "context",
    "cache",
    "learning",
    "bridges",
    "notifications",
    "files",
    "skills",
)


def selected_fields(value: object) -> tuple[str, ...]:
    """Ignore unknown fields; preserve an intentionally empty selection."""
    if not isinstance(value, list):
        return DEFAULT_FIELDS
    return tuple(key for key in FIELDS if key in value)
