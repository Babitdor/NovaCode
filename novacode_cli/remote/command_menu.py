"""Build Telegram's bounded slash-command menu without changing command semantics."""

from __future__ import annotations

import re
from collections.abc import Iterable


def build_command_menu(
    commands: Iterable[tuple[str, str]],
    skills: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Prioritize commands; expose kebab-case skills with Telegram-safe underscores."""
    result = []
    seen = set()
    entries = [*commands, *((name, f"Run skill: {name}") for name in sorted(skills))]
    for name, description in entries:
        name = name.replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]{1,32}", name) or name in seen:
            continue
        seen.add(name)
        description = " ".join(description.split())[:256] or name
        result.append({"command": name, "description": description})
        if len(result) == 100:
            break
    return result
